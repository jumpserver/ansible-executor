# OpenSSH 动态执行身份插件

此仓库实现专用 Ansible 执行容器的 **glibc NSS 插件**，配合未修改的 OpenSSH
使用。公钥认证交给 OpenSSH，身份查询交给 NSS，首次登录的家目录创建使用标准
`pam_mkhomedir`。另外通过小型 PAM **session** 模块管理任务 UID 的独占和清理；
认证仍交给 OpenSSH，无需给 OpenSSH 打补丁。

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
  因此业务授权必须由 Core/Celery 控制；SDK 按任务选择临时执行编号。
- 每套部署生成独立服务密钥，镜像不含任何客户端私钥或预置通用服务公钥。
  启动时只读挂载公钥；同一文件可列出多把公钥，便于轮换。
- SSH host key 保存在 `/var/lib/jms-executor`；该卷应持久化，客户端固定校验
  host key。服务公钥与 SSH host key 是两种不同的密钥。
- 家目录是 `0700`，凭据/产物默认 `umask 077`，临时目录在自己的家目录内。
  SSH 会话按虚拟 UID/GID 运行；入口验证无额外组及有效/许可/ambient capabilities，
  设置 `no_new_privs`，再用白名单重建环境。代码和认证配置只允许 root 修改。
- 不提供交互 shell、PTY、SFTP、用户 rc、用户环境配置、agent 或端口转发。
  只接受托管命令 `jms-executor-run <UUID>`、兼容命令 `ansible-runner worker`，
  以及只读探测命令 `jms-executor-info`。
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

Dockerfile 默认基于 `debian:trixie-slim` 构建，不依赖旧的 JumpServer Docker
执行镜像或 Core 源码。可用 `--build-arg BASE_IMAGE=你的镜像:版本` 替换兼容
Debian 的基础镜像；构建和运行阶段使用同一基础环境。其他发行版还需适配
系统包安装及 PAM/NSS 配置。
镜像提供 OpenSSH、PAM、NSS 插件、Python、JumpServer 定制的 Ansible/Runner、
业务 Collections 和驱动。默认构建不包含编译器、wheel 缓存和测试源码。
以下命令均从本仓库根目录执行。

```sh
docker build -t jumpserver/ansible-executor-ssh:latest .

mkdir -m 700 executor-secrets
ssh-keygen -q -t ed25519 -N '' -f executor-secrets/client
docker run -d --name jms-ansible-executor \
  --security-opt no-new-privileges:true \
  --memory 1g --pids-limit 256 \
  --tmpfs /run --tmpfs /home/jms-executor \
  --mount type=volume,src=jms-executor-host-key,dst=/var/lib/jms-executor \
  --mount "type=bind,src=$PWD/executor-secrets/client.pub,dst=/run/secrets/jms-executor.pub,readonly" \
  -p 127.0.0.1:2222:2222 \
  jumpserver/ansible-executor-ssh:latest
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

`requirements-runtime.txt` 定义镜像内的业务依赖，包括 JumpServer Ansible
`v2.16.18.5`、Runner `v2.4.0.2`、`ansible==9.13.0` 提供的 Collections，
以及 AWS、MySQL、PostgreSQL、SQL Server、MongoDB、Oracle、WinRM、RDP 等驱动。
系统运行库和编译依赖分别放在 Dockerfile 的 `base`、`build` 阶段。
Python 包在构建时安装，任务运行时不修改公共环境。
定制版 Ansible 默认限制 localhost/local connection；受信任的系统自动化
任务可单独传入 `ANSIBLE_SUPER_MODE=1`，不要在容器中全局开启。

`pyproject.toml` 继续只声明 SDK/执行器程序自身的依赖，Core 安装 SDK 不会
因此安装所有业务驱动。JumpServer 自定义模块仍由 Core 随任务传入，以保证
模块与 Playbook 一致。清单之外的 Collections、roles 或驱动需按业务补充。

镜像统一使用 `jumpserver/ansible-executor-ssh:latest`，独立于 Core 发版。
运行依赖或执行器变化后重建镜像，发布前回归任务执行、日志、停止及主要业务。
升级时显式拉取并重新创建容器；离线部署需加载更新后的镜像，不能只检查
本地是否存在 `latest`。部署记录应保留源码提交和镜像 digest，以便定位和回退。
CPU 兼容范围需要在目标硬件上验证，`linux/amd64` 标签不代表具体指令集基线。

## UID 生命周期与 Core 接入

仓库同时提供可安装的 Python SDK `jms-ansible-executor`。Core 的 SSH Runner
适配器使用该 SDK，原有原生 / Docker 后端仍可选择。配套 Core 的配置示例：

```yaml
ANSIBLE_EXECUTION_BACKEND: ssh
ANSIBLE_EXECUTOR_HOST: ansible-executor
ANSIBLE_EXECUTOR_PORT: 2222
ANSIBLE_EXECUTOR_PRIVATE_KEY: /run/secrets/executor_client
ANSIBLE_EXECUTOR_KNOWN_HOSTS: /run/secrets/executor_known_hosts
# 使用 Core 网域隧道时，设置为执行容器可访问的 Celery 地址。
# ANSIBLE_EXECUTOR_GATEWAY_HOST: celery-ansible
```

SDK 为每个任务单独连接并选择编号。服务端 PAM session 模块在 root monitor 中
持有 UID 独占锁；并发复用同一个 UID 会被拒绝，SDK 最多尝试 16 个候选编号。
任务拿到服务端租用确认前不发送目录或凭据。家目录在任务结束、取消、断连后
删除，并使用 pidfd 清理该 UID 的全部残留进程（包括 setsid 后台进程），再释放
锁。登录前也做清理，恢复崩溃 monitor 留下的进程和私有文件。进程清理失败或
文件无法删除时拒绝复用。探测命令结束后也会回收家目录。

除了家目录，清理器还删除 `/tmp`、`/var/tmp`、`/dev/shm`、`/dev/mqueue` 和
`/run/lock` 中属于该 UID 的对象，使用目录 FD 遍历且不跟随 symlink。不要挂载
额外的任务可写共享卷；派生镜像新增公共可写目录时须一并扩展清理策略。
Celery 队列的总并发数应按执行容器容量设置。单个常驻容器可处理多个不同 UID
的任务，不会为每次作业创建容器。

SDK 实时回传事件/状态并读取产物 rc，支持取消和超时。控制端严格校验 host key，
拒绝危险的事件文件名、路径穿越、符号链接产物和过大归档；不将远端 PID 或环境
传给 Core 状态回调。归档压缩大小上限 128 MiB、解压大小上限 512 MiB，任务输入
文件总量上限 128 MiB；输入不支持 symlink / 特殊文件。

```python
from jms_ansible_executor.client import run

result = run(
    host="ansible-executor", port=2222,
    private_key="/run/secrets/executor_client",
    known_hosts="/run/secrets/executor_known_hosts",
    private_data_dir="/private/job", inventory="/private/job/inventory/hosts",
    playbook="main.yml", quiet=True,
    event_handler=lambda event: print(event["event"]),
)
assert result.rc == 0
```

集成测试覆盖上游 Runner 2.4.0 和 Core 的 Runner 2.4.0.2 客户端与当前执行镜像
的 AdHoc、Playbook、上传、私钥路径及自定义模块兼容。SDK 迁移已知的 inventory 凭据字段、模块环境
路径和 copy 的本地 src，保留目的地址及普通业务值。所有输入必须预先放在任务
目录；不会自动读取或打包 Core 任意路径。Core 的完整配置和网域隧道要求见其
`utils/ansible_executor/ssh-backend.md`。

本实现仅应安装在专用执行容器内。`make install` 会安装专用 sshd PAM 配置，
`--configure-nss` 会设置该容器的身份查询顺序，不能直接用于宿主机 SSH。

## 验证

测试目标直接扩展生产运行层，使用相同的 OpenSSH、PAM、glibc 和 Ansible，
只额外安装编译器及复制源码用于 NSS 内存检查。
SSH 权限测试依赖 Python 标准库、Docker CLI、ssh 和 ssh-keygen；SDK 测试还需
安装本项目及其 Paramiko / Runner 依赖。

```sh
docker build --target test -t jumpserver-executor-ssh:test .
python3 -m pip install .
python3 -m unittest discover -s tests -p 'test_protocol.py' -v
python3 tests/integration.py
python3 tests/sdk_integration.py
```

测试用临时密钥和临时容器，不启动 privileged 模式、不挂载 Docker socket，
并固定校验服务器 host key。包括：NSS 全范围双向映射、非法用户名、缓冲区
边界与对齐；共享密钥并发登录不同 UID；错误密钥不创建家目录；拒绝 shell、
命令拼接和转发；真实 Runner 传输/执行/事件返回；跨 UID 私有文件和环境变量
读取被拒绝；任务环境不继承服务秘密、不污染后续会话。

CI 同时验证上游 Runner 2.4.0 和 Core 的 Runner 2.4.0.2 客户端。此测试覆盖通用 Ansible 执行及 UID
权限边界，不能替代 JumpServer 专用模块和业务作业的兼容性验证。
CI 还会构建默认生产镜像，并用该镜像运行同一套集成测试。
