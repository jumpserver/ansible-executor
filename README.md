# OpenSSH 动态执行身份插件

此仓库实现专用 Ansible 执行容器的 **glibc NSS 插件**，配合未修改的 OpenSSH
使用。公钥认证交给 OpenSSH，身份查询交给 NSS，首次登录的家目录创建使用标准
`pam_mkhomedir`。无需自定义 PAM 认证模块，也不需要给 OpenSSH 打补丁。

新增执行身份不需要 `useradd`、密码、逐用户公钥或写入 `/etc/passwd`：

| SSH 用户名 | UID/GID | 家目录 |
| --- | --- | --- |
| `jms_1` | `100001` | `/home/jms-executor/jms_1` |
| `jms_2` | `100002` | `/home/jms-executor/jms_2` |
| `jms_65535` | `165535` | `/home/jms-executor/jms_65535` |

只接受规范的 `jms_1` 至 `jms_65535`，拒绝 `jms_0`、`jms_01`、超范围数字、
路径字符及其他系统用户名；反向 UID/GID 查询得到同一个身份，不使用哈希映射。
NSS 查询无副作用，错误密钥的探测不会创建家目录。全量枚举不列出虚拟身份。
范围固定并保留给此容器，启动时发现已有账号或组冲突就拒绝启动。

## 认证与执行边界

- 所有虚拟用户使用同一份部署公钥。对应私钥 **只能由受信任的 Celery 服务持有**，
  不能交给平台终端用户或放入任务环境。持有此私钥就能选择任何虚拟身份，
  因此业务授权及用户/任务到身份编号的映射必须由 Core/Celery 控制。
- 每套部署生成独立服务密钥，镜像不含任何客户端私钥或预置通用服务公钥。
  启动时只读挂载公钥；同一文件可列出多把公钥，便于轮换。
- SSH host key 保存在 `/var/lib/jms-executor`；该卷应持久化，客户端固定校验
  host key。服务公钥与 SSH host key 是两种不同的密钥。
- 家目录是 `0700`，凭据/产物默认 `umask 077`，临时目录在自己的家目录内。
  SSH 会话按虚拟 UID/GID 运行；入口验证无额外组及有效/许可/ambient capabilities，
  设置 `no_new_privs`，再用白名单重建环境。代码和认证配置只允许 root 修改。
- 不提供交互 shell、PTY、SFTP、用户 rc、用户环境配置、agent 或端口转发。
  只接受原样的 `ansible-runner worker`，以及只读探测命令 `jms-executor-info`。
  Runner 的标准流二进制协议可以通过 SSH stdin/stdout 传输。
- 容器启动需要 **容器内 root** 以便 sshd 降权、PAM 创建家目录；不需要
  `--privileged`、`SYS_ADMIN`、Docker socket 或宿主机 PID namespace。
  这不是要求 `runAsNonRoot` 的部署方案。

此模块提供传统 Linux 多用户的文件和进程访问控制，不提供独立的 PID/network
namespace 或每任务 cgroup。默认 `/proc` 下，其他用户的进程名称、命令行等
元数据仍可能可见，**秘密不能放在命令行**。共享 `/tmp`、网络服务和可写公共
目录也不自动变成私有；任务必须将敏感数据写在自己的家目录，并为 localhost
服务配置认证。容器整体可设置内存、CPU 和 PID 上限。

## 构建运行

Dockerfile 直接基于通用 `debian:trixie-slim` 构建，不依赖现成的 JumpServer
镜像或 Core 源码。镜像提供 OpenSSH、PAM、NSS 插件、Python、`ansible-core`、
`ansible-runner`、SSH 客户端和 `sshpass`。当前入口使用 Runner 的流式 worker
协议，因此只安装 `ansible` / `ansible-core` 不够，还需要 `ansible-runner`。
默认构建不包含编译器和测试源码。以下命令均从本仓库根目录执行。

```sh
docker build -t jumpserver/ansible-executor-ssh:local .

mkdir -m 700 executor-secrets
ssh-keygen -q -t ed25519 -N '' -f executor-secrets/client
docker run -d --name jms-ansible-executor \
  --security-opt no-new-privileges:true \
  --memory 1g --pids-limit 256 \
  --tmpfs /run --tmpfs /home/jms-executor \
  --mount type=volume,src=jms-executor-host-key,dst=/var/lib/jms-executor \
  --mount "type=bind,src=$PWD/executor-secrets/client.pub,dst=/run/secrets/jms-executor.pub,readonly" \
  -p 127.0.0.1:2222:2222 \
  jumpserver/ansible-executor-ssh:local
```

通过可信部署通道取得 host key，而不是关闭 host key 检查：

```sh
docker exec jms-ansible-executor cat /var/lib/jms-executor/ssh_host_ed25519_key.pub \
  | sed 's/^/[127.0.0.1]:2222 /' > executor-secrets/known_hosts
ssh -T -p 2222 -i executor-secrets/client \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=executor-secrets/known_hosts \
  jms_1@127.0.0.1 jms-executor-info
```

将 `jms_1` 改为 `jms_2` 即可得到另一个 UID，不需要增加账号或配置公钥。
生产部署中 Celery 在容器网络连接此服务，端口无需发布到宿主机。

任务协议示意（控制端需要相同版本的 Ansible Runner）：

```sh
ansible-runner transmit ./job -p main.yml \
  | ssh -T -p 2222 -i executor-secrets/client \
      -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile=executor-secrets/known_hosts \
      jms_1@127.0.0.1 'ansible-runner worker' \
  | ansible-runner process ./job
```

该管道用于演示传输。业务调用方还需要检查各阶段状态和 Runner 的任务 rc，
处理超时、断线、取消、实时事件，以及上传文件、私钥等的路径映射。

## 作业依赖

通用镜像支持 Ansible 内置模块，不预装全部业务依赖：

- 额外 collections / roles：随任务打包，或者制作派生镜像预安装。
- JumpServer 自定义模块：由调用方随任务传递，并为当前任务配置模块搜索路径；
  SSH 后端接入时也要重写模块、私钥和上传文件的远端路径。
- 数据库、WinRM 等驱动：例如 `pymssql`、`psycopg2`、`oracledb`、`pywinrm`，
  以及它们依赖的系统库，按实际作业需要安装到派生镜像。
- Core 使用的 JumpServer Ansible/Runner 分支与这里的上游版本不是同一套依赖。
  集成前需要验证兼容性；如依赖特定分支，应在派生镜像中安装匹配版本。

默认 Runner 为 `2.4.0`，可通过 `--build-arg ANSIBLE_RUNNER_VERSION=...` 指定
其他上游版本。控制端和执行端应使用匹配的 Runner 版本；版本变更后重跑集成测试。

## UID 生命周期与 Core 接入

此插件不修改现有 Core 的执行后端，也不自动关闭 `ANSIBLE_DOCKER_ENABLED`。
当前 Core 仍然使用原来的 Runner；切换到 SSH 后端需要单独接入。

推荐由调度方为每个作业分配独立编号，而不是把同一用户所有不同授权的作业
放到同一 UID。**同一编号始终代表同一 UID**，NSS 不负责调度、并发锁或清理。
不要把尚有文件或后台进程的 UID 分给另一个安全主体。Runner 正常结束会清理
其默认临时工作目录，但任意 Playbook 仍可写入其他私有文件或启动后台进程；
断开 SSH 也不能替代完整清理。UID 回收前需要受信任的管理进程确认所有相关
进程已退出并删除私有数据。此版本没有提供 UID 回收接口。

本实现仅应安装在专用执行容器内。`make install` 会安装专用 sshd PAM 配置，
`--configure-nss` 会设置该容器的身份查询顺序，不能直接用于宿主机 SSH。

## 验证

测试目标直接扩展生产运行层，使用相同的 OpenSSH、PAM、glibc 和 Ansible，
只额外安装编译器及复制源码用于 NSS 内存检查。
测试客户端只依赖宿主机的 Python 标准库、Docker CLI、ssh 和 ssh-keygen。

```sh
docker build --target test -t jumpserver-executor-ssh:test .
python3 tests/integration.py
```

测试用临时密钥和临时容器，不启动 privileged 模式、不挂载 Docker socket，
并固定校验服务器 host key。包括：NSS 全范围双向映射、非法用户名、缓冲区
边界与对齐；共享密钥并发登录不同 UID；错误密钥不创建家目录；拒绝 shell、
命令拼接和转发；真实 Runner 传输/执行/事件返回；跨 UID 私有文件和环境变量
读取被拒绝；任务环境不继承服务秘密、不污染后续会话。

生产与测试均默认使用上游 Runner 2.4.0。此测试覆盖通用 Ansible 执行及 UID
权限边界，不能替代 JumpServer 专用模块和业务作业的兼容性验证。
CI 还会构建默认生产镜像，并用该镜像运行同一套集成测试。
