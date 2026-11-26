# 园区上行降速 · 告警/录像带宽保障调速台

用 **Flask + Docker Compose + tc HTB + 两路 UDP 试发** 演示并验证：园区上行降速后，
**告警**与**录像**两类流量各自保有保底带宽，空闲一路时另一路可借用余量；
网页可在线调整总速率与两类保底/上限，调整全程不重建队列；任一步骤失败会自动、
安全地回退到改前配置并显示原因；可一键演示“调速中途失败”。

---

## 1. 快速开始

```bash
docker compose up --build -d
# 首次构建需要 apt 安装 iproute2、pip 安装 Flask/waitress
xdg-open http://localhost:8080      # 或浏览器手动访问
```

宿主机 8080 被占用时：

```bash
WEB_PORT=8081 docker compose up --build
```

停止：

```bash
docker compose down            # 加 -v 同时删除状态卷
```

> 需要 Docker Engine + Compose v2。服务使用 `privileged: true`，因为要创建
> network namespace / veth 并下发 `tc qdisc/class/filter`（需要 `CAP_SYS_ADMIN`
> 与 `CAP_NET_ADMIN`）。仅限本地实验环境使用，页面无鉴权，请勿直接暴露到公网。

容器健康后，页面顶部后端徽标应显示 **“后端：真实 tc”**。

---

## 2. 隔离链路与 HTB 拓扑

`docker compose up` 启动一个 `linklab` 服务，`service/entrypoint.sh` 调
`python -m linklab.setup_lab` 自动创建一条与宿主网络隔离的 Linux 链路：

```
        network namespace lab-src                network namespace lab-dst
 ┌───────────────────────────────────────┐    ┌──────────────────────────────┐
 │ uplink 10.77.0.1/24                   │veth│ downlink 10.77.0.2/24        │
 │  root qdisc 1:  htb default 20         │◄──►│  UDP receiver :5001 (告警)    │
 │  class 1:1      rate=ceil=总速率 T     │pair│               :5002 (录像)  │
 │   ├─ 1:10 alarm rate=g1 ceil=c1 prio1 │    │  实测速率/丢包 → recv_stats   │
 │   └─ 1:20 video rate=g2 ceil=c2 prio2 │    └──────────────────────────────┘
 │  u32: udp dport 5001 → 1:10           │
 │       udp dport 5002 → 1:20           │
 │  UDP sender(网页分别启停)             │
 └───────────────────────────────────────┘
```

- 整形发生在发送端 `lab-src` 的 `uplink` **egress**，即“园区上行”方向。
- 默认配置（降速后）：总速率 `T=10 Mbit/s`，告警保底 `g1=2 Mbit/s`，
  录像保底 `g2=6 Mbit/s`，两者 `ceil` 都是 `10 Mbit/s`。
  因此任一类空闲时，另一类最多可借用到 `10 Mbit/s`；两类同时打满时，
  各自至少得到保底，剩余带宽按 HTB 规则分配。
- 网页“开始”会在 `lab-src` 内启动定速 UDP 发包进程（默认每路 12 Mbit/s
  承诺负载，确保压过整形速率；可在输入框改，`0` 为不限速打满），
  接收端在 `lab-dst` 内按 1 秒窗口统计实测 Mbit/s、收包数与序号丢包。

---

## 3. 网页功能

| 区域 | 功能 |
| --- | --- |
| ① 速率配置 | 修改总速率、两类的保底 `rate` 与上限 `ceil`；输入时即时做约束校验；可“预览安全步骤”；可“应用调速（发包中也可）” |
| 故障注入 | 可勾选“在第 N 条命令失败”，方式可选**模拟失败**或**真实坏命令**（给 tc argv 追加非法 token，由真实 tc 报错）；另有 **⚡ 一键演示中途失败并回退** |
| 变更结果 | 逐步展示每条命令（含完整 argv）、rc、stderr、当时生效总速率、该步不变量是否成立；失败时展示原因与完整回退步骤 |
| ② 试发 | 告警/录像分别 **开始/停止**，互不影响；显示实测 Mbit/s、发送速率、收包数、序号丢包、PID、吞吐条与迷你曲线；支持两路同开/全停 |
| ② 之 DSCP 换标核验 | 告警每轮试发从 **DSCP 0 / ECT(0)（TOS 0x01）** 开始；网页填入 1～63 的新 DSCP，**发送途中切换一次**，发端不中断、序号接着走、ECN 两位保留；页面显示新标记起用的**分界序号**；收端读实收 IPv4 TOS 按分界分两段核对（晚到旧包仍归旧段），分段显示实收 DSCP/ECN 与错标包数，新段收到包前显示“待确认”；换标失败则继续用旧标记并显示原因。全程**有界内存** |
| ③ 内核配置 | 实时显示 `tc -s -d qdisc/class show`、`tc -s filter show`、`ip -d link`、netns 列表与 ping；5 秒自动刷新，可手动刷新 |

调速只使用 `tc class change ...`（根类 `1:1` 与子类 `1:10/1:20`），
**不会** `tc qdisc del` 或重建类，因此原 HTB 队列句柄 `1:` 及其统计队列持续保留。
可在调速前后对比内核区的 `qdisc htb 1:` 与类统计来确认。

---

## 3.5 DSCP 途中换标核验（园区下游交换机按 DSCP 分队列的前置验证）

下游交换机准备按 DSCP 给告警分队列，本台先在隔离链路上核验“换标”本身是否
端到端生效。功能只作用于**告警流**（UDP/5001），录像流照常试发、不受影响。

### 标记与切换

- 每个告警发送 socket 以 `setsockopt(IP_TOS, 0x01)` 起始，即 **DSCP 0 / ECT(0)**。
- 网页“DSCP 换标核验”框可填 **1～63** 的新 DSCP，点“发送途中换标”。
  发端 `udp_send.py` 的后台线程轮询控制文件，在**发包不中断、序号接着走**的情况下
  再次 `setsockopt(IP_TOS, (new_dscp<<2)|ECN)`，**ECN 两位原样保留**。
- 每轮试发（一次开始→停止）**只允许切换一次**；发端与后端各自硬性保证。
  停止后重新开始即回到 DSCP 0 / ECT(0)、序号从 0 重新计。
- 发端把“第一个使用新标记的报文序号”作为**分界序号 `boundary_seq`** 写入统计，
  后端把它发布给收端计划文件 `mark_plan_alarm.json`。
- 若 `setsockopt` 失败（或 DSCP 非法）：**继续使用旧标记、继续发包（录像照常）**，
  页面与 API 显示原因，不产生分界计划。

### 收端核验（有界内存）

- 收端 `udp_recv.py` 对每个 socket 置 `IP_RECVTOS`，用 `recvmsg` 辅助数据读取
  **实收 IPv4 TOS 字节**（无需 raw socket / libpcap），拆出 DSCP（高 6 位）与 ECN（低 2 位）。
- `dscp.MarkVerifier` 按发端分界把报文分两段：`seq < boundary` 为旧段、
  `seq >= boundary` 为新段。**晚到的旧段报文按序号仍归旧段**，不会被算成错标。
- 为消除“计划文件晚于新标记报文到达”的竞态，收端维护一个**定长环形缓冲**
  （默认 2048 个 `(seq, TOS)`，可配），计划到达时按分界重放缓冲、精确重分段；
  环形之外的报文必然属于旧段。配合两段定长计数器与封顶的“实收 TOS 分布”表，
  **无论收多少包，内存占用都有上界**，不做逐包无限堆积。
- 分段输出：期望/实收 DSCP、ECN 名、包数、DSCP 错标数、ECN 错标数、错标合计、
  实收 TOS 分布。**新段在收到首个包前显示“待确认（pending）”**，收到后转“已确认”。
- TOS 无法获取（平台不支持 `IP_RECVTOS`）计入 `unknown_tos`，不猜测、不谎报通过。

### 操作路径（网页）

1. 在告警流卡片点“开始”，看到旧段按 DSCP 0 / ECT(0) 计数、错标 0；
2. 填新 DSCP（如 20）点“发送途中换标”，横幅提示分界序号；
3. 收端分段表出现“新段”，首个新包到达前为“待确认”，随后转“已确认”且错标 0；
4. 再次换标会被拒绝（每轮一次）；停止后开始新一轮即复位。

API：

```bash
curl -s -X POST localhost:8080/api/flows/alarm/start -H 'Content-Type: application/json' -d '{"rate_mbps":4}'
curl -s -X POST localhost:8080/api/flows/alarm/remark -H 'Content-Type: application/json' -d '{"new_dscp":20}'
# -> {"ok":true,"old_dscp":0,"new_dscp":20,"ecn":1,"boundary_seq":919,...}
curl -s localhost:8080/api/flows   # flows.alarm.rx.marking 内为分段核验结果
```

---

## 4. 核心安全不变量与“每一步”排序

后端所有速率统一用 **整数 kbit/s** 表示。对“当时在内核生效的总速率 `T_eff`”，
每个状态（包括调整与回退的每个中间步）都必须满足：

```
g_alarm + g_video ≤ T_eff
ceil_alarm        ≤ T_eff
ceil_video        ≤ T_eff
g_alarm ≤ ceil_alarm,  g_video ≤ ceil_video     # HTB 自身要求
```

`linklab/planner.py` 是纯逻辑的“安全迁移规划器”，把任意合法的
`(T,g1,c1,g2,c2)旧 → 新` 拆成若干条 `tc class change`，并保证每一步后
不变量都成立。排序规则：

- **总速率上调**：先提高根类 `1:1` 总速率 → 再移动两类；
- **总速率下调**：先把两类按“目标的更紧包络”收好 → 最后才降低根类总速率；
- 两类内部固定四阶段：① 先降 `rate`（释放保底预算）→ ② 降 `ceil`
  → ③ 升 `ceil` → ④ 最后升 `rate`。

这样可证明：任何中间状态的保底之和都不超过当时总速率，且 `rate ≤ ceil`。
规划器对 4000 组随机合法新旧配置做了逐步不变量 + 终态一致性 + 反向（回退）校验。

### 失败与回退

`TcManager.apply()` 在同一把锁内逐步执行：

1. 目标不合法 → **一条命令都不执行**，返回 409 与错误；
2. 任一步命令 `rc != 0`（或故障注入触发）→ 立即停止；
3. 以“最后一个已成功步骤对应的已知状态”为起点，用**同一个安全规划器**
   生成回到“本次改前配置”的回退计划并逐步执行；
4. 页面显示失败命令、真实 stderr 原因、回退每一步与最终配置；
5. 若回退命令本身也失败，会明确标记 `drift`，提示内核配置可能漂移，
   不会谎报成功。状态文件 `<LAB_RUN_DIR>/lab_config.json` 只在完全成功或
   回退成功后落盘为改前/目标值。

---

## 5. HTTP API 摘要

```bash
curl -s localhost:8080/api/state
curl -s -X POST localhost:8080/api/config/validate -H 'Content-Type: application/json' -d '{...}'
curl -s -X POST localhost:8080/api/config/apply    -H 'Content-Type: application/json' \
  -d '{"total_mbps":8,"alarm_rate_mbps":1.6,"alarm_ceil_mbps":8,
       "video_rate_mbps":4.8,"video_ceil_mbps":8}'
# 中途在第 2 步注入“真实 tc 坏命令”失败：
curl -s -X POST localhost:8080/api/config/apply -H 'Content-Type: application/json' \
  -d '{...,"fault_step":2,"fault_mode":"real"}'
curl -s -X POST localhost:8080/api/demo/failure
curl -s localhost:8080/api/kernel
curl -s localhost:8080/api/flows
curl -s -X POST localhost:8080/api/flows/alarm/start -d '{"rate_mbps":12}' \
  -H 'Content-Type: application/json'
curl -s -X POST localhost:8080/api/flows/video/stop
# 告警途中换标（每轮一次，1~63；失败保留旧标记并返回原因）
curl -s -X POST localhost:8080/api/flows/alarm/remark \
  -H 'Content-Type: application/json' -d '{"new_dscp":20}'
```

---

## 6. 目录结构

```
docker-compose.yml          # privileged linklab 服务 + 8080 + /run/lab 卷
service/
  Dockerfile                # python:3.11-slim + iproute2 + flask + waitress
  entrypoint.sh             # setup_lab 后 exec CMD
  requirements.txt
  linklab/
    model.py                # 配置模型 + 不变量校验
    planner.py              # 纯逻辑安全迁移/回退排序（核心）
    tc_argv.py              # 拓扑 + 全部 tc/ip argv 构造（纯函数）
    runner.py               # SubprocessRunner / FakeRunner / CmdResult
    tc_manager.py           # 预览、应用、故障注入、回退、内核报告、历史
    setup_lab.py            # 建 netns/veth/HTB/filter（容器启动一次）
    flows.py                # 两路 sender + receiver 进程管理、换标控制与状态合并
    dscp.py                 # TOS/DSCP/ECN 原语 + 有界环形缓冲分段核验器（纯逻辑）
    udp_send.py             # 独立定速 UDP 发包脚本（IP_TOS 打标/途中换标，stdlib）
    udp_recv.py             # 独立接收/测速/丢包/DSCP·ECN 分段核验脚本（stdlib）
    stub.py                 # 无 CAP_NET_ADMIN 时的内存 tc 模拟后端
    web.py                  # Flask 应用与 API
    templates/index.html
    static/style.css, app.js
tests/                      # 55 个测试（规划器/回退/Web/真实 loopback 收发/DSCP 换标核验）
scripts/run_tests.sh
```

### 无 Docker / 无 tc 环境的本地开发模式

后端可切换为内存 stub，链路退化为 loopback，此时除“真实整形”外的
网页、API、进程管理、实测速率、失败回退都能真实运行：

```bash
python3 -m pip install -r service/requirements.txt
LAB_BACKEND=stub LAB_SRC_NS= LAB_DST_NS= LAB_DST_IP=127.0.0.1 \
LAB_RUN_DIR=/tmp/lab python3 -m linklab.web
# http://127.0.0.1:8080
```

---

## 7. 交付前验证结果（如实记录）

构建本交付物的沙箱本身是一个**无 `CAP_SYS_ADMIN/CAP_NET_ADMIN`、无 docker
CLI/daemon、无 tc/ip、且禁止 unprivileged user namespace** 的容器
（`CapEff=0`，`unshare -Urn` 返回 `Operation not permitted`）。
因此**无法在此环境真正执行 `docker compose up` 或验证 HTB 整形/借用的实测数值**。
我做了以下可在该环境完成的高强度验证：

1. **单元/集成测试 55 项全部通过**
   `PYTHONPATH=service python3 -m unittest discover -s tests`
   - 不变量校验（保底和、ceil、rate≤ceil、边界）；
   - 安全规划器排序、4000 组随机合法新旧配置的逐步不变量/终态/反向路径；
   - 中途 stub 故障与“真实坏 token”故障后的自动回退、回退失败 drift 上报；
   - 成功调速只出现 `tc class change`，**绝不出现 `qdisc del`**；
   - Flask 全部 API + 页面渲染（含换标 API 的 404/409/503 与 DSCP 面板渲染）；
   - **真实子进程 UDP loopback 测试**：8 Mbit/s 定速发送 4 s，接收端实测
     `rate_mbps > 2`（实际约 8.0）、收包 >200、序号丢包 0；
   - **DSCP 纯逻辑核验器（`test_dscp.py`）**：TOS 字节拼装/拆分全 64×4 组合、
     1～63 输入校验、按分界分段、晚到旧包归旧段、错标（DSCP/ECN）计数、
     “计划晚到”环形缓冲重分段、环形上限驱逐与有界性、unknown/无头包处理；
   - **真实子进程换标端到端（`test_dscp_loopback.py`）**：loopback 上真实
     `IP_TOS`→`IP_RECVTOS`，途中换 DSCP 20/46，发端报分界、收端两段实收 TOS
     全对、错标 0、ECN 全程 ECT(0)；并用 `--reorder-every` 注入乱序，验证
     **晚到旧包仍归旧段**；非法 DSCP(99) 被拒、发端保留 DSCP 0 且继续发包；
   - **发端 `MarkerController`（`test_marker_controller.py`）**：起始 0x01、
     成功换标保 ECN 并锁分界、`setsockopt` 失败时保留旧标记并上报原因、
     非法 DSCP 拒绝、每轮硬性只切一次；
   - **FlowManager 集成（`test_flow_remark.py`）**：真实进程 start→换标→
     收端 verified、一轮一次守卫、停止清理计划文件、重启后可再次换标。
2. **真实 iproute2 6.1.0 二进制校验命令语法**：下载并解包 Debian
   `iproute2` 及其依赖，对规划器/setup 生成的 **109 条 tc argv** 逐一用真
   `tc` 解析（在 `lo` 上跑到 netlink 阶段）。全部通过解析，仅在最后
   `RTNETLINK answers: Operation not permitted`（沙箱缺权限，属预期）；
   故障 token 被真 tc 以
   `What is "__lab_inject_failure__"?` / rc=1 拒绝，证明“真实坏命令”演示有效。
3. **真实 HTTP 端到端（stub + loopback）**：用 `python -m linklab.web` 实启服务，
   curl 验证：两路同开实测均为 **8.002 Mbit/s、0 丢包**；发包过程中成功应用
   10→8 Mbit/s 五步变更；`/api/demo/failure` 正确返回失败原因、第 2 步失败、
   1 步回退成功并恢复“该次改前”配置；`fault_mode=real` 返回真实
   `unknown option "__lab_inject_failure__"`；单独停止告警后其速率归零、
   录像仍保持 8.002；页面/CSS/JS 均 200，`node --check app.js` 通过；
   并用 waitress 以容器同款 WSGI target 启动验证 200。
   - **换标实测（同一套真实进程）**：告警 4 Mbit/s 发送中 POST
     `remark {new_dscp:20}` 返回 `ok:true, boundary_seq:919, ecn:1`；收端核验
     旧段 919 包实收 TOS 全为 `0x01`（DSCP0/ECT0）、新段 978 包实收 TOS 全为
     `0x51`（DSCP20/ECT0），`verified:true`、`total_mismatch:0`、
     `unknown_tos:0`；再换 DSCP 30 时新段 TOS 全为 `0x79`（=30<<2|1）。
     二次换标被拒（每轮一次，HTTP 409）、DSCP 0/99 被拒（409）、未知流 404、
     未运行换标 409；停止后 `mark_*` 文件清理，重启新一轮可再次换标；
     录像流 `plan_installed:false`，不受影响。
4. `docker-compose.yml` 通过 YAML 解析与字段人工核对；shell 脚本具备可执行位。

### 需在具备 Docker 的目标机上补做的实测（命令与预期）

```bash
docker compose up --build -d
docker compose ps                       # healthy
# 1) 借用：只点“告警 开始”，预期告警实测接近其 ceil（默认 10 Mbit/s，受发送 12 压满）
# 2) 保底：再点“录像 开始”，两者同时打满，告警>=2、录像>=6（允许 HTB 精度误差）
# 3) 在线调速：两路发送中把总速率改 8、保底 1.6/4.8、ceil 8，观察吞吐与
#    “内核配置”中 1:1/1:10/1:20 rate/ceil 立即变化，而 qdisc handle 仍为 1:
# 4) 故障：点“一键演示中途失败并回退”，查看原因与回退；刷新内核配置确认恢复
# 5) DSCP 换标：告警“开始”后填新 DSCP（如 20）点“发送途中换标”，预期横幅给出
#    分界序号，收端分段表新段由“待确认”转“已确认”，两段错标 0、ECN 恒为 ECT(0)；
#    可在 lab-dst 内用 tcpdump 复核：ip netns exec lab-dst tcpdump -vv -i downlink -c 5 'udp port 5001'
docker exec -it gsb-linklab tc -s class show dev uplink      # 需经 netns：
docker exec -it gsb-linklab ip netns exec lab-src tc -s class show dev uplink
```

---

## 8. 已知限制 / 未完成工作（诚实说明）

- **未在真实 Docker/内核中跑过端到端 HTB 整形与借用**：当前沙箱没有 docker
  daemon 且无 `CAP_NET_ADMIN`/user namespace，这部分只完成了命令语法级
  （真实 tc 解析器）与逻辑级（测试 + stub/loopback）验证。首次在目标机
  `docker compose up` 时需按第 7 节清单确认 `sch_htb/cls_u32` 自动加载与实测带宽。
- 为保证 `ip netns`/tc 一次成功，compose 使用了 `privileged: true`，未收敛到
  细粒度 capabilities / AppArmor 配置。
- 页面无鉴权、无多用户锁之外的审计持久化（最近 10 条变更仅存内存环）；
  状态 JSON 仅保存“期望配置”，内核原始配置以 `tc` 输出实时展示，未做差异解析。
- HTB 在两路同时超额时的剩余带宽分配受 prio/quantum/包长影响，文档只承诺
  “不低于保底 + 可借用到 ceil”，未承诺精确的超额切分数值。
- 未提供 IPv6 分类规则（u32 仅匹配 IPv4 UDP dport）；实验地址固定为
  `10.77.0.0/24`，可通过 `LAB_*` 环境变量调整。
- DSCP 换标核验当前只在**告警流（UDP/5001）**网页开放（录像流保留同一套收端
  计数但不提供换标按钮，符合“先核验告警分队列”的范围）；每轮仅允许切换一次，
  不支持在一次试发内连续多次换标。
- 收端用 `IP_RECVTOS`/`recvmsg` 读 TOS，已在本机 loopback 实测可真实读回；
  若某平台不支持该辅助项，会计入 `unknown_tos` 而不是伪造通过。环形缓冲默认
  2048 项（约亚秒级窗口），正常换标远在该窗口内；极端情况下若计划晚于环形深度
  才到，被驱逐的报文按旧段处理并计 `ring_evicted`，内存仍严格有界。
- DSCP 功能尚未在真实 veth/下游交换机链路抓包复核（沙箱无 `CAP_NET_ADMIN`/
  docker），目标机应按第 7 节清单用 `tcpdump -vv` 再确认一次 DSCP/ECN 位。
