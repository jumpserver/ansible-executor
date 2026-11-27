# JumpServer Ansible 执行器

[English](README.md) · 简体中文

通过 SSH 在专用的常驻容器中运行 JumpServer Ansible 作业。每个任务使用独立的
临时 Linux UID。Celery 可以并发运行任务，并实时接收任务事件和退出码。

## FAQ

### 应该使用哪个镜像？

正式部署使用 `jumpserver/ansible-executor:<version>`，版本号与 Core 一致，
省略 `-ce` 或 `-ee` 后缀。开发部署使用对应的分支标签。
`latest` 保留给每个任务单独启动容器的旧版 Core。

### 任务之间如何隔离？

每个 SSH 会话使用 `jms_1` 至 `jms_65535` 中的一个身份，独占对应的非 root UID
和私有家目录。执行器在复用 UID 前清理任务进程和文件。任务共享容器的网络、
进程命名空间和资源上限。其他 UID 可能看到进程元数据，请勿将凭据放入命令行参数。

### 任务可以上传什么？

将 Playbook、Inventory、模块和任务凭据放在任务私有目录内，只使用普通文件和目录。
任务输入上限为 32 GiB，返回产物上限为压缩后 128 MiB、解压后 512 MiB。
按并发任务数为归档和解压文件预留磁盘空间。

## 快速开始

在仓库根目录运行以下命令，需要 Docker 和 OpenSSH 客户端。SSH 服务在容器内以
root 启动，任务以非 root 身份运行。会话清理要求 Linux 内核和容器策略允许通过
PID 文件描述符发送信号（`pidfd_send_signal`）。

### 1. 构建并启动执行器

```sh
docker build -t jumpserver/ansible-executor:local .

mkdir -m 700 executor-secrets
ssh-keygen -q -t ed25519 -N '' -f executor-secrets/client

docker run -d --name jms-ansible-executor \
  --security-opt no-new-privileges:true \
  --memory 1g --pids-limit 256 \
  --tmpfs /run --tmpfs /home/jms-executor:exec,nosuid,nodev \
  --mount type=volume,src=jms-executor-host-key,dst=/var/lib/jms-executor \
  --mount "type=bind,src=$PWD/executor-secrets/client.pub,dst=/run/secrets/jms-executor.pub,readonly" \
  -p 127.0.0.1:2222:2222 \
  jumpserver/ansible-executor:local
```

挂载卷用于在更换容器后保留 SSH 服务端主机密钥。家目录的 tmpfs 需保留 `exec`，
以便运行随任务上传的脚本。

### 2. 验证 SSH 连接

从运行中的容器直接读取主机公钥，再通过 SSH 检查执行身份。

```sh
docker exec jms-ansible-executor cat /var/lib/jms-executor/ssh_host_ed25519_key.pub \
  | sed 's/^/[127.0.0.1]:2222 /' > executor-secrets/known_hosts

ssh -T -p 2222 -i executor-secrets/client \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=executor-secrets/known_hosts \
  jms_1@127.0.0.1 jms-executor-info
```

命令返回 JSON，其中 `uid` 和 `gid` 均为 `100001`。

## 接入 JumpServer Core

标准 Installer 部署已有默认连接配置。自定义部署时，将 Celery 和执行器放在
私有网络内，2222 端口仅供内部访问，并确保执行器能连接目标资产。每套部署单独
生成服务密钥。该密钥可登录任意执行 UID，因此私钥只交给受信任的 Core/Celery
服务，执行器只挂载公钥。

将私钥和通过可信渠道获取的 `known_hosts` 文件挂载到 Core/Celery 的 `config.yml`
所指定的路径。`known_hosts` 条目使用实际连接地址，例如 `[ansible-executor]:2222`。

```yaml
ANSIBLE_DOCKER_ENABLED: true
ANSIBLE_EXECUTOR_HOST: ansible-executor
ANSIBLE_EXECUTOR_PORT: 2222
ANSIBLE_EXECUTOR_PRIVATE_KEY: /run/jms-executor/client
ANSIBLE_EXECUTOR_KNOWN_HOSTS: /run/jms-executor/known_hosts
```

使用网域 SSH 隧道时，将 `ANSIBLE_EXECUTOR_GATEWAY_HOST` 设置为执行器可访问的
Core/Celery 地址。详见 [Core SSH 后端文档](https://github.com/jumpserver/jumpserver/blob/dev/utils/ansible_executor/ssh-backend.md)。

## 开发

镜像内置 JumpServer 定制的 Ansible、Runner、Collections 和驱动，版本见
[requirements-runtime.txt](requirements-runtime.txt)。Core 的自定义模块随任务上传。

Python SDK `jms-ansible-executor` 提供 [`client.run()`](src/jms_ansible_executor/client.py)
来提交任务。在 Python 3.11 或更高版本的虚拟环境中运行本地检查。

```sh
python3 -m pip install .
python3 -m unittest discover -s tests -p 'test_*.py' -v
docker build --target test -t jumpserver-executor-ssh:test .
python3 tests/integration.py
python3 tests/sdk_integration.py
```

镜像发布和集成检查见[构建工作流](.github/workflows/build.yml)。

## 许可证

Copyright (c) 2014-2026 FIT2CLOUD, All rights reserved.

本项目基于 GNU General Public License version 3（GPLv3）许可，与 [JumpServer 主仓库](https://github.com/jumpserver/jumpserver)一致。完整许可文本请参阅 [LICENSE](LICENSE)。
