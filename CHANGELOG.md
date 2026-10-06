# Changelog

### v2.12.2：Claude 周额度或会话额度用完时，run 进入限额暂停并显示重置时间，不扣调用次数（RL-WEEKLY）；efficient 模式下，回合进行中插件正常升级不再作废这个回合（FIELD-24）；review-pr 移植的后续内部批次

- **fix（RL-WEEKLY，poker-news-bot WI-109 现场）**：Claude author 回合撞上周额度，run 以笼统的 "CLI exit 1" HOLD，扣掉了一次调用，也没有重置时间。原因是 `classify_rate_limit_failure` 不认识 stream-json 里的限额形态。现在以下任一信号都确认为额度限制：
  - `rate_limit_event` 的 `status` 为 `rejected`，`resetsAt` 转成 "resets at <本地时间> (<窗口> window)"；
  - CLI 合成的 assistant 错误消息（`is_api_error_message`，`error` 属于 rate_limit 一类）；
  - `api_error_status` 为 429 的错误 result；
  - "You've hit your <usage|weekly|session|5-hour|opus|…> limit"，文中的 "resets <时间>" 作为提示。
  - 只看最后一个 `rate_limit_event`：在 extra usage（overage）上运行时，`rejected` 不算额度用完，后续出现 `allowed` 或 `allowed_warning` 也会取消之前的 `rejected`。
  - 普通 CLI 错误照旧计费，退出码 0 永远不算额度限制。测试夹具是 WI-109 那个回合真实的最后三行 stdout。
- **fix（FIELD-24，poker-news-bot WI-109 现场）**：v2.12.1 发版时 lane 重启改写了 `installed_plugins.json`，FIELD-21 的 efficient 规则因此作废了一个跑了 78 分钟的 EXEC author 回合并重派。现在：
  - efficient 模式下，回合中出现 FIELD-21 认定的正常插件升级（只改 version、installPath、gitCommitSha、lastUpdated，且新路径是真实存在的规范 cache 目录）时，记录为 `global_config_changes.plugin_update`，回合保留、不重派。正在运行的 CLI 启动时已加载插件，旧版本 cache 目录也还在，所以回合的工作不受影响。
  - receipt 里写明 `next_turn_registry`，下一个回合用新的插件注册表。
  - strict 模式不变（HOLD，带 `PLUGIN_UPDATE_HINT`）。其他注册表改动，或同一回合还触发了别的检查，照旧是硬错。
- **review-pr 移植（D-LG2），内部实现，尚未接入任何 skill**：
  - LG2-a3：report run 在 POLISH-Q 之后进入 report 版 SECURITY，以新的终态 REPORTED 结束（退出码 0）。
    - 敏感路径和 secret 预检只产出 finding，不拦截。只有本次改动带进、且仍在候选树中的才算 CRITICAL；仓库原有的或被本次改动删除的降为 MINOR。
    - 每次 HOLD 都把报告标为不完整，并写明原因和未完成的阶段。
    - 调用预算是角色数的 2 倍，在候选树上冻结。
  - LG2-b1：在 REPORTED 和每次 HOLD 时，从 state 和 ledger 渲染 `review-report.md`，不经过模型。新增两个只出报告的 specialist：comment-analyzer 和 type-design-analyzer。新增 `--aspects`（仅 report 模式，默认 all），创建时冻结。
  - LG2-d：`scripts/materialize_pr.py` 解析并钉住审查输入（PR 编号或 URL、本地或远程 ref），在临时 clone 中检出钉住的 PR head。
    - 所有 git 调用都限定协议（`GIT_ALLOW_PROTOCOL=https:ssh:file`）。
    - 用 URL 寻址的调用（`ls-remote`、`clone`）在空的临时目录里运行，仓库本地配置无法借此执行命令。
    - PR 里的符号链接按普通文件检出。
  - 这些开关仍未写进文档，请勿使用。
- **审查**：都走 ABA 审查链，Opus 写、Codex 逐轮审、fresh Opus 终审。按 owner 10-06 的新原则，审查只拦现实中会发生的误用和正常使用下的误报；刻意绕过、刻意构造的本地状态和最坏情况记为残余。
  - RL-WEEKLY：Codex 1 轮 APPROVE；Opus 终审打回 1 条（overage 误判），修完后修复审查轮 APPROVE。
  - FIELD-24：Codex 1 轮 APPROVE，Opus 终审 APPROVE。
  - LG2-a3：Codex 2 轮；Opus 终审打回 2 MAJOR 和 1 MINOR，已修；修复审查轮又打回 1 条，按 owner 选 (a) 再修一轮，剩下的"含 secret 的路径换成空目录或符号链接"按新原则记为残余。
  - LG2-b1：Codex 3 轮，Opus 终审 APPROVE；3 条 MINOR 并入 LG2-b2。
  - LG2-d：Codex 2 轮；Opus 终审打回 1 HIGH，修完后修复审查轮 APPROVE。

### v2.12.1：用独立 CODEX_HOME 的 run 不再因默认 ~/.codex/config.toml 里别人的 trust 条目而 HOLD（owner P0）；fresh shadow/gate 不再把仓库原有文字当成泄漏的审查记录（FIELD-23）；EXEC 等待 reviewer 时可以 note；smoke runner 被杀后不留残局；owner 决策单落档；review-pr 移植的设计与前两批内部实现

- **fix（owner P0，poker-news-bot 现场）**：run 用独立 `CODEX_HOME` 时，也会对默认 `~/.codex/config.toml` 做快照，用来发现无视 `CODEX_HOME` 的 Codex 子进程。原先这个文件有任何变化，都会作废已完成的回合并 HOLD。而另一个项目在默认 home 上跑 Codex（或有人还原备份）时，Codex 会自动追加 `[projects."<dir>"] trust_level = "trusted"`，于是 WI-108 丢了 19 分钟和 13 分钟的 author 回合。现在：
  - 默认配置的变化如果只是增删整张外来 trust 表，即路径不是本 run 自己的 workspace、clone 或 worktree 主根，就记为 `foreign-default-home-trust-entry {added, removed}`，不作废回合。Codex 回合和 permission probe（包括 probe 正常结束的路径）都适用。
  - 其他变化照旧 HOLD。下列情况也照旧 HOLD：
    - 本 run 自己路径的 trust 条目（比较前先做 realpath、去掉尾部斜杠，macOS 上不区分大小写）；
    - 无法按 UTF-8 解码的内容；
    - 超过 1 MiB 或结构有歧义的文件。
  - 扫描只过一遍：已有 3000 张表时也在 1 秒内完成。
  - 不变的部分：`$CODEX_HOME/config.toml` 本身的检查、在默认 home 上跑的 run、uncertain 回合，行为都不变。
  - 建议：每个 run（包括验收 run 和监工 run）都使用独立的绝对路径 `CODEX_HOME`。实测 codex-cli 0.160.0 在 git 目录里以 workspace-write 运行时一定会写 trust 条目，`-c` 和 `--ignore-user-config` 都挡不住。
- **fix（FIELD-23，poker-tools 现场）**：仓库里本来就有的注释（例如 "gate finding"）被作者改动后，出现在 delta patch 的 -/+ 行里，fresh shadow 的独立性检查把它当成泄漏的审查记录，run 因此 HOLD；改写注释也没用，因为旧文字留在 "-" 行里。owner 选了简单规则：
  - 有 `base_commit` 时，patch 只扫描 "+" 行；"-" 行和上下文行都不扫。
  - "+" 行里的命中，如果整段命中文字按整词在 base 中同一文件（或重命名前的文件）里出现过，就豁免；新文件和二进制文件不豁免。
  - delta.stat 和 status.txt：命中落在 base 已有路径里就豁免；stat 改为完整路径，并且不合并重命名。
  - plan.md 和 workitem.md：只豁免用反引号或代码块引用的 base 原文。
  - prompt 和 gate 模板永远不豁免。fresh shadow、gate、PLAN 批准和 review-only 创建共用同一个 helper。
  - 威胁模型：只防无意中带入审查记录，不防作者刻意绕过。已知残余：base 中已有的标记词在同一文件里任何位置都豁免；不用 `git mv` 的移动按新文件处理（偏严）。
- **note**：EXEC 在等 reviewer（或它的 shadow）而 HOLD 时，现在接受 `note`，只交给下一个 EXEC author 回合，不会进任何审查角色。PLAN 阶段等 reviewer 时仍然拒绝，并提示替代做法。
- **smoke runner（SMOKE-RUNNER-KILL / SMOKE-TIMEOUT）**：
  - runner 被杀后会停掉 case 进程组并恢复临时配置，不留残局。
  - 以 nohup 方式或在后台作业中启动时，继承下来的 SIGHUP 忽略状态会被保留。
  - case 超时只在机器有负载时放宽，最多 6 倍；空闲时不变。SMOKE-TIMEOUT 的根因没有查清，详见 smoke README 和 ADR-4 的修订。
  - 已知残余：marker 不校验 temp_config 的 sha，孤儿进程组还活着时也不拒绝运行。
- **review-pr 移植（D-LG2），内部实现，尚未接入任何 skill**：
  - 设计文档 `paired_session/docs/review-pr-port.md` 已经 owner 答复并通过终审。
  - 前两批是 coordinator 里的 report mode，入口是 `run --review-only --review-report`：
    - 创建、resume 和反馈时的拒绝规则；
    - 不论 EXEC 和 gate 给出什么结论，都依次走到 gate 和 POLISH-Q，全程没有任何 writer 回合；
    - 每次派发前都核对冻结的树；
    - report run 上的 permission-probe 不跑 author probe（标为 NOT-APPLICABLE）。
  - 目前 report run 在 POLISH-Q 之后停在临时 HOLD，SECURITY、REPORTED 和报告文件由后续批次补齐。这个开关还没有写进文档，请勿使用。
- **决策记录**：ADR-13 D-OWNER-1005（owner 对 2026-10-05 决策单的 59 项答复；D11 各行为暂定）及其补充条款：终审后的修复审查、D12（review-only 的 max_exec_rounds 保持 4）、ABA/BAB 审查链。
- **测试**：`tests/evidence_ledger_test.py` 的一条断言不再依赖 git 自己的 hook 报错原文（新版 git 措辞不同）。
- **CI（D08）**：GitHub Actions 新增 tests/ 的 pytest job，Linux 和 macOS 都跑。
- **审查**：采用 ABA/BAB 审查链。
  - FIELD-23：Codex 2 轮，加 Opus 终审和终审后的修复审查轮。
  - LG2-a2：Codex 1 轮，加 Opus 终审和修复审查轮。
  - LG2-a1：BAB，Codex 写、Opus 2 轮、Codex 终审，加修复审查轮。
  - smokefix 的修复审查轮没过，按 owner 裁定由监工复核后收下。
  - P0：Codex 2 轮，加 Opus 终审（1 HIGH、1 MEDIUM、2 LOW）和终审后的修复审查轮（APPROVE）。

### v2.12.0：legacy 工作流正式弃用（owner 裁定 D-READY）；同一台机器上的并发 run 不再因对方的 Codex trust 条目而 HOLD（FIELD-22）；M7 评分与冻结工具

- **弃用（D-READY，owner 2026-10-05）**：
  - owner 按现场证据裁定 ADR-6 的替换门槛已经满足，legacy 工作流从 v2.12.0 起标为 deprecated。现场证据有两条：poker-news-bot 和 poker-tools 连续多天在 paired-session 上做真实工作并交付；owner 记得的那次额度用完后 reset 续跑，事后查实是 Codex 额度、发生在试验版协调程序上；它暴露的两个问题（限额暂停没有类型、失败重试占用调用上限）v2.11.0 已修。新流程从中断恢复并跑完的证据是 poker-news-bot 的 WI-86。owner 看过证据后维持裁定。
  - 显式选用 legacy 时会打印一行弃用提示。显式选用指：`entry: legacy`、`/review-loop:legacy`、Codex 的 legacy 请求，或直接调用 `/review-loop:plan` / `/review-loop:execute`。路由和行为都没有改。
  - 删除 legacy 代码要等三项都完成：review-pr 迁到 paired-session（D-LG2）；code-quality-loop 并入 `run --review-only`（Q6）；legacy 对照表中 18 个 owner 行由 owner 定夺。在此之前，review-pr 和 code-quality-loop 仍走 legacy。
  - M7 种子缺陷对比不再是退役门槛，改为可选的成本/质量研究。不计分的试点显示，paired 的首轮 review 成本约为 legacy 的 6%，墙钟约为 1/12。
  - 相关文档：DECISIONS.md 中 ADR-6 新增修订，迁移文档新增 "Deprecation status (v2.12.0)"，`paired_session/docs/legacy-deprecation-readiness.md`；README 和 guide 同步更新。
- **fix（FIELD-22，supervisor 并行真实 run 发现）**：两个 run 共用默认的 `~/.codex` 时，一个 run 的 Codex 回合会给自己的 workspace 写 trust 条目，原来会让另一个 run 的 Codex 回合误判为全局配置被改而 HOLD。现在，如果某个 workspace 属于另一个正在运行的 paired-session run，它的 trust-only 追加记为预期改动 `trusted-concurrent-run-workspace`，并附上那个 run 的 id。"正在运行"需要同时满足：有本用户私有的 workspace lease、该 run 的 state 写的是同一个 workspace、coordinator lock 的 pid 与 lease 一致且进程还活着。uncertain 回合的 `resume --acknowledge-codex-trust` 也接受这类条目。其他任何改动仍然是硬 finding。仍然推荐每条 lane 用独立的 CODEX_HOME。
- **M7 工具（不影响产品行为）**：采纳了 Dot 的评分修复 21/21b，lane B 又补了几项加固：对案例仓库做 git 隔离；DUPLICATE 不能链式引用；冻结时记录原始字节摘要，skip-worktree 和 eol 转换都藏不住改动。冻结时会保留被跟踪但匹配 .gitignore 的文件；拒绝放在 `.compass/results` 下的语料。D-b1 扫描器停在评审上限，没有包含在本版里。
- **文档**：Q6 code-quality-loop 备忘、legacy 退役就绪度报告（附 overseer 成本测量）、大任务定义提案。
- **审查**：Codex gpt-6.1-sol。field22 3 轮，m7-s1 3 轮，m7-s1b 2 轮，deprecate 2 轮。

### v2.11.1：插件正常更新不再让回合 HOLD（FIELD-21）；`--timeout` 设上限；author 在被忽略路径写的可执行配置会在报告里列出；legacy → paired-session 对照表

- **fix（FIELD-21，poker-news-bot 现场报告）**：回合进行时，如果另一个 Claude Code 会话把 review-loop 插件更新到新版本（`~/.claude/plugins/installed_plugins.json` 里同一插件条目的 version、installPath、gitCommitSha、lastUpdated 一起变），原来会把这次改动算到 author 头上并 HOLD。现在只要能确认是正常更新，efficient 模式下就作废这个回合，并在新的 baseline 上自动重跑一次；strict 模式下 HOLD，并提示可以 resume。识别条件很严：新 installPath 必须正好是插件缓存里新版本的目录，从缓存根目录往下每一级都必须是真实目录、不能是链接，其他字段不能有任何变化。只要有一项不符，仍按原来的硬 finding 处理。被作废的回合照样计入调用次数，和已有的作废重派路径一致。
- **改进（timeoutcap）**：`--timeout` 限定在 1 到 86400 秒之间，默认仍是 2700。超出范围时，在创建任何 lease、run 目录或 detach 子进程之前就直接拒绝。
- **改进（f3，B 类，只报告）**：author、FINISH、DOCS 或 POLISH-Q 修复回合在被 git 忽略的路径里新建或修改了可执行配置文件（`.vscode/tasks.json`、`.vscode/launch.json`、`.claude/settings*.json`、`.claude/commands/**`、`.mcp.json`、`.envrc` 等）时，会写进 receipt 和 state、打印 WARNING，并在 `status --brief` 和交付报告里列出来。两种模式都不 HOLD。清单只看元数据，扫描有上限。
- **fix（startup40）**：`tests/protocol_loading_graph_test.py` 的 Claude one-file-delivery 启动削减比例，从 v2.9.7 起一直低于 40% 门槛（39.84，后来降到 39.68），现在是 40.45%。修法是把 `loading.md` 里两段很少用到的规则原样移到按需读取的 `docs/protocol/loading-special-cases.md`，再把各入口 skill 里重复 `loading.md` 的措辞改成指针。规则一条没删，测试和门槛都没改。
- **文档**：`docs/paired-session-migration.md` 新增"Legacy → paired-session map (after v2.11.0)"，共 49 行：已覆盖 27 行，已排进计划 4 行，等 owner 决定 18 行。F6（一家厂商的回合期间另一家的全局配置被改，只记录、不 HOLD）在 1C 安全控制表里登记为接受的残余风险。
- **审查**：Codex gpt-6.1-sol。field21 2 轮，startup40 1 轮，timeoutcap 2 轮，wrapperdedupe 1 轮，legacy-map 3 轮，f3 2 轮。最后一轮都是 APPROVE。

### v2.11.0：paired-session 支持"只审已有代码"（`run --review-only`），默认入口的审查请求不再落回 legacy；可选的 `--detach`/`stop`；限流 HOLD 带类型

- **新功能（D-LG1：只审已有代码的入口）**：
  - `bin/paired-session run --review-only [--base REF]` 审查工作区里已有的改动，不经过 PLAN，第一步就是 EXEC review。已有改动算作 EXEC 第 1 轮，所以 `--max-exec-rounds N` 恰好是 N 次审查。base 默认为 `HEAD`，也就是审查未提交的改动；审查分支时传 `--base main` 或某个 merge-base。
  - 创建时直接拒绝的情况：base 不是 HEAD 的祖先、没有改动、index 有冲突或部分暂存、unborn HEAD、`--stop-after-plan`。第一次 review 之前树或范围变了，就 HOLD。
  - 各角色的提示词改用 review scope，不再出现"已批准的计划"；author 只修复审查提出的问题。
  - 交付：baseline 取 review base 的树，被审改动记为本次交付；完全暂存的改动属于被审范围；被审改动已经改过的 docs 文件算它自带的（pre-owned）；提交信息带 `Review base: <oid>`。完整 lifecycle（FINISH、POLISH-Q、DOCS、SECURITY、accept/auto_commit）照常可用。
  - **默认入口**：没有 `entry` 键时，"审查我的改动"这类请求（code-exists）也交给 paired-session 的 `run --review-only`，不再落回 legacy。只有与任务相关的改动才这样路由；工作区里还有无关改动时先问用户。已有计划、已有 session 仍走 legacy。legacy 的 `execute --review-only` 在退役前保持不变。
  - Q1–Q9 的选择（`run --review-only`、base 默认 HEAD、经 skill 走完整 lifecycle、base 不是祖先就拒绝、已有改动算第 1 轮等）由 supervisor 按设计文档的建议临时采用，等 owner 确认。每项都集中在一个常量里，改起来只动一处。
- **新功能（detach，FIELD-17 后续）**：`run|resume|reject|permission-probe --detach` 让命令脱离宿主会话运行（setsid 加两次 fork），日志写到按用户隔离的临时目录，调用方立刻拿到 pid 和日志路径。用 `stop` 结束：先作废当前回合并回收它的进程组，然后按中断处理（`resume --retry-uncertain` 或 `abort`）。默认行为不变。Claude 宿主规则写明，无交互会话里优先用 `--detach`。实测宿主怎么结束命令：Claude Code 的 `claude -p` 交卷时给后台命令的进程组发 SIGTERM，Codex 直接 SIGKILL 命令所在的会话；脱离后的进程都不受影响。
- **改进（ratelimit）**：任何角色、任何阶段被 provider 限流，都会停在一个带类型的 HOLD（`hold_kind: rate_limited`，附角色、阶段和 reset 提示）。被限流的调用不计入调用次数，也不占 DOCS、SECURITY 和重派预算；`resume` 后不重复、也不跳过回合。
- **fix（F7）**：`resume --retry-uncertain` / `permission-probe --retry-uncertain` 恢复期间如果全局配置变了，会在下一次派发的 baseline 处 HOLD，比较和恢复之间不再留有窗口。
- **测试（igncache）**：允许的测试命令在已存在的被忽略缓存目录（`__pycache__`、`.pytest_cache`）里写东西，不会让只读回合作废。新测试证明了这一点，产品代码没改。
- **审查**：每个单元都由 Codex gpt-6.1-sol 审查（D-REV-CODEX）：
  - LG1 共 6 个单元（a1、a2、b、c、d、e），各 1 到 3 轮，最后都是 APPROVE 或 APPROVE_WITH_FINDINGS（只剩 LOW）；
  - detach 3 轮，ratelimit 2 轮，f7 3 轮，igncache 2 轮。
- **已知限制**：
  - review-only：base 里非 UTF-8 的文件名、DOCS 的删除和重命名、writer 的暂存拒绝，只写进了文档或测试，没有完整覆盖。
  - detach 没有真实宿主上的端到端自动测试；记录目录不会自动清理；用 SIGKILL 结束脱离的命令会留下回合的进程组，请用 `stop`。
  - F6：一家厂商的回合期间另一家的全局配置变了，仍然只记录、不 HOLD。现有测试固定了这个设计；宿主 Claude Code 自己就会写 `~/.claude`。这是接受的残余风险，"只在有证据表明是本回合造成时才 HOLD"作为提议留给 owner。
  - `tests/protocol_loading_graph_test.py` 的 40% 启动削减测试（claude 一文件交付）在 v2.9.7 起就低于门槛（39.84，现在 39.68），之前没进 CI 所以没被发现。修复已排期，门槛和测试都不改。

### v2.10.1：耗时长的测试命令等跑完再判定（FIELD-20）；run 开始时提醒 .gitignore 覆盖不全（FIELD-19）；只读回合改动权限位可检测（READONLY-PERM）

- **fix（FIELD-20，poker-news-bot 现场报告）**：配置的测试命令超过约 10 秒时，Codex 会让命令转入后台。原来 permission-probe 的提示词规定每条命令只调用一次 `exec_command`，模型就不会回头轮询；回合结束时命令被杀，记录里只有开始、没有退出码，结果被报成 `allowed-command-failed`。用真实 codex-cli 0.160.0 已复现：一条 120 s 的命令在回合结束时被杀。
  - reviewer、gate、docs reviewer、specialist 和 probe 的提示词都加了轮询规则：用 `write_stdin` 发空输入、`yield_time_ms` 30000 轮询，拿到退出码之前不得结束回合，轮询不算额外命令。Claude 侧要求长命令的 Bash 调用把 timeout 设为 600000。真实 CLI 实测 120 s 和 150 s 的命令都等到了 exit 0。
  - 只有 item.started、没有 item.completed 的 Codex 命令，现在记为"已开始、回合结束时没有退出码"。probe 报 `allowed-command-not-completed (no exit status: still running or killed when the turn ended)`；EXEC 审批 HOLD 和 DOCS reviewer 的报错会注明"the configured test was not observed to completion"，不再报成测试失败。
  - evidence guard 只放行字面的、不带输入的轮询 cell。A/B 类要求不变：仍要观察到配置命令完整跑完一次且退出码为 0。
- **改进（FIELD-19，发版前真实 run 发现）**：
  - run 开始时（efficient 下是 `run`，strict 下是 `permission-probe`）就按 SECURITY 预检的同一套规则检查已提交的 `.gitignore`。有缺失类别时打印 WARNING 并写入 `lifecycle.ignore_coverage_at_start`，提醒先提交覆盖这些类别的 `.gitignore` 再开跑。只警告，不阻止。
  - SECURITY 因覆盖不全 HOLD 后，可行的恢复办法是：在工作树里改 `.gitignore`，既不提交也不暂存，然后 resume。这会重放 EXEC 审查，修改随交付一起提交。已提交的话 HEAD 会移动而 HOLD，把 HEAD 恢复到 run 的 parent 并保留修改即可；已暂存的话 accept 时会拒绝，需要先取消暂存。HOLD 原因和文档都写明了这些。
- **fix（READONLY-PERM，Codex 跨厂商复审 B2）**：只读回合把 tracked 或未被忽略的 untracked 普通文件的权限位改了（例如 0644 改成 0600），现在能检测到，处理方式和执行位改动相同：作废该回合、恢复、校验、重派一次。evidence 里会有 `mode: <path> 0644 -> 0600`。交付时的 mode 仍取自 manifest。
- **审查**：每项都由 Codex gpt-6.1-sol 审查（D-REV-CODEX：审查改由 Codex 做，Claude 只写代码和协调）。FIELD-19：R1 APPROVE_WITH_FINDINGS，R2 APPROVE。READONLY-PERM：R1 REQUEST_CHANGES（2 个 MEDIUM），R2、R3 APPROVE。FIELD-20：R1 APPROVE_WITH_FINDINGS，R2 APPROVE。
- **已知限制**：
  - 轮询规则只写在提示词里，模型仍可能不遵守；不遵守时会如实报"没跑完"。上限是每次派发的 `--timeout`，以及 Claude 单次 Bash 调用最长 10 分钟。
  - 权限位检查不覆盖被忽略文件、symlink、目录、ACL、xattr 和 file flags。

### v2.10.0：paired-session 成为默认入口（完整 lifecycle，默认 efficient 安全模式）；`entry: legacy` 可退回旧流程

- **升级须知**：
  - **默认入口变了。** 没有 `entry` 键时，新的 `/review-loop`（Claude）或 review-loop 请求（Codex）交给 paired-session，并打印一行默认入口提示。想继续用 legacy：在 `.review-loop/config.md` 写 `entry: legacy`（v2.9.x 上也有效，可以先写再升级），或用 `/review-loop:legacy`（Codex 说 "use the legacy review-loop workflow"）。已有 plan、代码或 session 的工作，以及 `/review-loop:plan`、`execute`、`review-pr`，仍走 legacy。
  - **前提条件。** 需要 macOS、各角色所需的 CLI（默认 Codex 当 author 和 gate、Claude 当 reviewer）、一个专用 worktree 和测试命令。缺了任何一项：没有 `entry` 键时带提示回退 legacy；写了 `entry: paired-session` 时直接拒绝。
  - **默认安全模式是 efficient。** 和 strict 相比只少 C 类：
    - 派发前不要求 permission-probe PASS；
    - 不要求 Claude author 的 opt-in 或 probe；
    - 不做 Codex CLI 沙箱契约校验；
    - `--accept-*` 豁免照收，但不记录（打印 NOTE）；
    - 证据守卫只记 `would_hold`，不拦截。
    所有沙箱、Codex 能力扫描、全局配置检查、敏感环境变量屏蔽、"必须观察到测试命令"、run 目录在 workspace 之外，这些两种模式都照旧。
  - **strict 怎么开。** `--strict`，或在 operator profile 写 `"safety_mode": "strict"`；workspace 里的配置不能设置它。模式在创建 run state 时冻结：不同的模式会被拒绝，只有"只跑过 probe"的 run 可以用 `--strict` 升级。strict 的 lifecycle run 拒绝 `--accept-unverified-claude-author` 和 `--accept-probe-skip`（D-7）。
  - **旧 run 按 strict 恢复。** v2.9.x 上启动的 run 没有 `safety_mode`，恢复时按 strict 处理，保持 `lifecycle_mode=off`，到 DONE 结束。Claude author 的 probe PASS 绑定插件版本：这类 run 在 `resume`/`reject` 前要重跑一次 `permission-probe`。另外，默认的 gate prompt 文件属于程序绑定，插件安装路径变了也会要求重跑。不要在 run 进行中换版本。新的 efficient run 不需要 probe。
  - **同一棵树会被重新审查。** 快照现在把可执行位记为 `exec:<sha>`。所以跨版本 resume 时：含可执行文件的树，旧的批准和 accept intent 会失效（要重新审查）；被旧版本拒绝过的这类树，`rejected_digests` 认不出来。auto_commit journal 从未在正式版里出现过；内部候选版留下的旧 journal 会 HOLD，只能 abort。
- **feat（默认入口，v210-entry / U0–U6，E-1..E-12）**：两份 paired-session skill 默认走 efficient：直接 `run`，不先跑 probe；strict 仍是先 probe 再 run。skill 总是传 `--lifecycle-mode on`（D-4）。`auto_commit` 只从 operator profile 读取，`.review-loop/config.md` 里的 `auto_commit: true` 只会警告（E-4）。agent 只在你在对话里明确接受后才执行 `accept`，handsfree 下从不执行（E-6）。不再标注 "experimental"（E-12）。
- **feat（worktree lifecycle W，lane A W1a–W3c）**：EXEC 收敛之后，依次在同一 worktree 里跑 FINISH → POLISH-Q（三个 specialist，各自负责自己的 finding）→ DOCS（docs writer 加 docs review，review 必须观察到测试）→ SECURITY（`sensitive_policy` 和 `security_preflight.py` 两个扫描，加一个 fresh security reviewer），然后到 DONE 等待接受。任何一步改了树，都从 EXEC 审查和 gate 重放。
- **feat（accept / reject / auto_commit）**：
  - `accept --expect` 的 intent 绑定所有阶段 receipt，并写一份中文 `delivery-report.md`；W run 拒绝 `--override-rejection`。`reject` 重开 EXEC。
  - operator profile 设了 `auto_commit: true` 时，接受会做一次不触发 hook 的本地提交，内容正好是被接受的 manifest，从不 push。流程是先写 journal，再用 CAS 移动 ref（6878582 起绑定 accept 时的分支和 index；文件权限取自 manifest，不取自当前 lstat），最后同步 index。
  - 中途崩溃后可以用 `accept --expect <journal digest>` 补完。被取代或已 ABORTED 的 run 不能重放（w3c）。
  - FINISH/DOCS writer 改了 HEAD、分支或 index 会 HOLD（writer guard），HOLD 信息保留回合的原始错误。
- **feat（D-EFF A/B 类，两种模式都生效）**：
  - **只读保护**：reviewer、gate 或 shadow 回合改动了工作区（包括只改权限的 chmod），这个回合作废，判定不采用。差异存到 `evidence/`。先确认进程组已停，再按回合前的记录恢复，并核验 HEAD、分支、index 和树；然后重派一次。W 阶段的这次重派计入该阶段的上限。第二次再改动，或恢复、捕获失败，都会 HOLD。
  - **恢复失败**：恢复失败的记录写进 `unrestored_readonly_turn`。只要它还在，`run`、`resume`、`reject`、`accept`（包括 `--override-rejection`）和任何 `--scope-change` 都会被拒。手动把工作区恢复原样后，记录自动清除；否则只能 abort。
  - **author 守卫**：author 回合改变 HEAD 或分支（commit、reset、checkout）一律 HOLD。
- **feat（证据守卫 eg-wire / eg-cwd）**：
  - 每个工具调用都按类型分类，只有 PROTECTED 才 HOLD；像 workspace 里 `tests/evidence/` 这类误报不再 HOLD。解析不了的调用退回原来的子串守卫，次数记在 receipt 里。
  - Codex 命令的工作目录改从 rollout 读取，前提是同一命令的各条记录一致。
- **fix（probe）**：
  - 只有敏感环境变量名字变了时，probe PASS 仍然有效（FIELD-10）；PATH 或程序变了仍要重跑，拒绝信息会写明哪一项变了。
  - 失败的 probe 报告在名字变化后仍算负面证据（env-name）。
  - probe 允许执行的命令被 Claude CLI 超时杀掉时，报 `allowed-command-timeout (<N> s)`（FIELD-12）。
  - 同一父目录下的 run 共用一把 probe 锁，Claude author probe 树改名为 `paired-session-claude-probe-tree-*`；锁一直被占时，在写任何 state 之前报 REFUSED（FIELD-13）。
- **fix（审查历史，FIELD-11）**：plan 里带审查历史（例如 F001 这类 ledger 编号）时，在 PLAN 批准那一刻就拦下，不再拖到最后的 gate。如果是最后一轮 PLAN，author 额外得到一次只改写、不计轮数的回合，内容写到 `plan-rewrite.md`。
- **fix（operator 证据，修复 v2.9.5 的已知限制）**：author 回合看过的每一棵树（包括失败的回合、被杀的回合），其上的 operator verification 都作废。
- **fix（v210-field）**：CLI 回合仍在运行或状态不确定时，`accept` 拒绝，提示先收尾或 abort；`--help` 里 attach-verification 的参数归成一组。
- **chore（models-b，MEDIUM-3）**：
  - Claude 的 reviewer backstop 和 cheap-tier 默认值改为 `claude-opus-5-5`；`.codex/agents` 和 runtime regression 的 Codex 默认值改为 `gpt-6.1-sol`；`docs/install-codex.md` 要求 codex-cli 0.159.2 或更新。
  - report-only agent 的 frontmatter 保留 Bash，但只用于直接调用；launcher 路径仍然只读（文档对齐，MEDIUM-3）。
- **refactor（v210-dedup）**：两份 paired-session 入口 skill 的共用规则移到 `docs/protocol/paired-session-entry.md`，通过 `read_protocol.py --stage entry-paired-session` 加载；SKILL.md 只留宿主相关的规则。行为不变。
- **fix（lane C eff-e）**：Codex 跨厂商审查提出的 A 类问题，两种模式都生效：
  - `safety_mode` 不能由 workspace、run 目录或 author 临时目录里的 `--config` 设置，创建时和 probe-only 升级时都一样；
  - 只读回合改动或删除了已有的被忽略条目（按 git 列出的条目记录类型、大小、mtime、权限、链接目标）时，判定作废并 HOLD，不恢复、不重派；条目数或耗时超过上限时，receipt 写明未检查；
  - 回合结束后工作区读不出来（例如 `.git/index` 损坏）时，按无法验证的只读违规处理：写入恢复阻断记录，恢复原样前派发和 accept 都被拒绝。
- **fix（发版前真实 run 发现，rel210-fixCG）**：Codex 能力扫描不再因为 `$CODEX_HOME/plugins/cache` 里的插件 bundle（例如 Codex 给每个 ChatGPT 登录自动装的 chatgpt-global 插件）而 HOLD。coordinator 每次派发 Codex 角色都带 `-c features.plugins=false`，这些 bundle 不会生效；扫描把它们记为 inert 并写进 receipt。去掉这个启动参数时，扫描照旧报出来。config.toml 里的 MCP/apps、项目配置、requirements 和 MDM 检查不变。之前用默认 `~/.codex` 走 Codex 角色时，第一次派发就会 HOLD。
- **fix（发版前真实 run 发现，FIELD-15 / FIELD-16）**：Claude 侧的 run 目录从 `${CLAUDE_PLUGIN_DATA}`（在 `~/.claude` 下，Claude Code 视为敏感路径，每次都要确认，无交互时直接被拒）移到 `${XDG_STATE_HOME:-~/.local/state}/review-loop/runs/<UUID>/`。建 run 目录、写 WORKITEM.md 等准备步骤失败时，一律报 `stage A failure: <reason>`，默认入口按文档回退 legacy 并打印提示，不再悄悄换流程。无交互的 `claude -p` 会话结束回合时会杀掉后台的 run，驱动不了 paired-session，请用交互式会话（FIELD-17，已写进 skill 和迁移文档）。
- **fix（发版前真实 run 发现，FIELD-18）**：入口 skill 加载协议时，原来把 `.review-loop/tmp/protocol-*.md` 写进产品工作区；这些未跟踪文件会被当成本次改动的一部分去审查、可能被 auto_commit 提交，并触发 shadow 的独立性检查而 HOLD。现在 review-loop 入口和 paired-session 入口（Claude、Codex 两边）把协议写到系统临时目录下按用户隔离的目录里；共享入口规则写明 run 开始前产品工作区里不能有入口 skill 留下的文件。
- **已知限制**：
  - 只读回合把 tracked 文件的非执行权限位改掉（例如 0644 改成 0600）检测不到。git 不记录这些位，它们不会被审查或交付，只读沙箱是第一道防线。
  - 被忽略条目的检查只看条目本身的元数据：折叠目录内部的改动、记录之外新增的被忽略文件检测不到；CLI home 或测试缓存放在 workspace 的被忽略路径里会让每个只读回合 HOLD，请放到 workspace 之外。
  - auto_commit 绑定的是 accept 那一刻的分支（由 `--expect` 授权），不是 run 开始时的分支。
  - candidate-tree 隔离（独立候选检出、coordinator 测试沙箱）仍是之后的加固项；没有 Compass BACKLOG close 阶段；不提供 push、PR、merge。
  - `accept`/`reject` 没有 operator 身份认证；DONE 不等于接受。
  - 干净的 Claude-author probe PASS（M6 残留项）只在 strict 下需要，不是本版的发版条件（D-EFF）。
  - 候选测试沙箱只在 macOS 上可用。
- **审查与验证**：
  - 每个批次 Claude Opus 审查最多 2 轮。Codex gpt-6.1-sol 跨厂商审查（只在本版做）分三部分：W lifecycle 与交付、efficient 模式与 A/B 类、入口与文档；前两部分提出的 7 个问题由 rel210-fixA 和 eff-e 修复，复审 6 个 FIXED，1 个 PARTIAL（见已知限制）。
  - 按 E-1（2026-10-04 修订），发版门槛是 GitHub Actions 全绿 + 1 次经默认入口、lifecycle on、走到 ACCEPTED 的真实 run；跨厂商审查只在本版做。

### v2.9.7：可选的整个工作项截止时间（FIELD-2）；同一类阻断连续三次就停下（FIELD-5）；Codex 默认模型改为 gpt-6.1-sol（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：
  - Codex 角色（author、reviewer、gate）没有显式指定模型时，默认改为 `gpt-6.1-sol`（ADR-12）。这需要 codex-cli 0.159.2 或更新：版本更低或读不出时，`run`、`resume`、`permission-probe` 在创建 run state 之前就拒绝，提示升级 CLI，或对取了默认值的角色显式传 `--author-model/--reviewer-model/--gate-model gpt-6-luna`。版本只从已通过程序绑定的 codex 路径读取，绝不执行 workspace 指定的程序。
  - 已经开始的 run 保留 state 里冻结的模型，不迁移，也不做版本检查；`abort`、`status` 照常可用。
  - author 模型是 permission-probe 的键的一部分：依赖默认值的运营者升级后，每个 run 目录要重跑一次 `permission-probe`（旧 PASS 和探测缓存不再命中）。
  - codex-cli 0.160.0 加入已验证版本（v2.9.6 发版时的真实 permission-probe：Codex reviewer 和 Codex gate 都 PASS）。
- **feat（FIELD-2，v297-f2 / f2b）**：新增可选的 `--wi-deadline SECONDS`，默认关闭，从 run 开始按墙钟计时（HOLD、等待、重启都计入）。截止之后：
  - 不再启动新的回合，在下一次派发处 HOLD；正在跑的回合不会被打断，单回合超时不变；
  - 截止时间在 `run` 时固定，`resume` 沿用保存值、拒绝不同的值；
  - 截止只拦新派发，绝不改写已有的 HOLD，也不拿掉 owner 的出口：`resume` 和 `permission-probe` 在不改任何东西的情况下被拒，round-limit HOLD 上的 `accept --override-rejection` 等出口都保留；还没收尾的不确定回合仍可用 `--retry-uncertain` 确认进程组已结束并归档，然后可以 `note --scope-change`；
  - 已到 DONE 的 run 仍可 `accept` 或 `reject --scope-change`；`reject` 和 `resume --polish` 被拒，因为它们的下一次派发只会 HOLD；
  - 墙钟回拨超过 60 秒时也 HOLD，已用时间不退还；
  - scope-change 产生的后继 run 不继承截止时间，需要显式传入。
- **feat（FIELD-5，v297-f5）**：EXEC reviewer、shadow 和 gate 的提示要求每个阻断 finding 以 `[class: <kebab-case-name>]` 开头。coordinator 只用正则读取这个显式标签，不做文本相似度判断。
  - 同一类别在连续三次 EXEC reviewer 或 gate 的阻断里都出现时，run 先把下一回合交回 author，然后 HOLD "structural fix / re-scope needed"，并列出每次阻断的 finding 编号。
  - 转去 gate 的批准、只带旧阻断项的 REVISE 不算打断；真正结束审查的批准会打断计数；HOLD 之后从头计数。
  - 不关闭、不接受任何 finding；round-limit HOLD 和它的 override 优先。
  - 没带标签的阻断项只计数（`unlabeled_blocking_findings`，run 合计和每个回合的 receipt），永不 HOLD。
  - gate 的标签保留在 ledger summary 开头，persistent reviewer 能看到并复用。
- **fix（v297-cwd，发版前真实 run 发现）**：在不是 git 仓库的目录里启动 run 时，第一次审查之后生成 review-mirror diff 的那条 git 命令在协调器自己的当前目录里执行；git 2.40 起在仓库外拒绝 `--attr-source`，run 在下一次派发前 HOLD（`cannot use --attr-source or GIT_ATTR_SOURCE without repo`）。现在这条命令在 workspace 里执行。这个问题从 v2.9.x 起就存在，之前的真实 run 都是在仓库里启动的。
- **test（v297-tscale）**：测试里 10 秒以内的固定超时和 settle 等待改为随负载放大（系数 = clamp(load1/CPU 数, 1, 6)，可用 `PAIRED_SESSION_TEST_TIMEOUT_SCALE` 覆盖）。产品侧只认这个显式变量，未设置时生产超时完全不变。
- **test（v297-linux-zombie）**：一个只在 Linux 上失败的测试改为像生产一样回收自己的进程组 leader（Linux 上只剩僵尸的进程组仍会回应 `killpg(pid, 0)`）；原来只靠它覆盖的"EPERM 视为已结束"分支另加了 mock 测试。
- **test（CI-TESTFIX）**：测试不再依赖开发机：
  - 本机没有 codex/claude 时用测试自带的存根，守卫仍拦截真实 CLI；
  - 候选测试的工作区带一个最简单的测试（Python 3.12 起，unittest 找不到测试时退出码为 5）；
  - 91 个依赖 macOS sandbox-exec 的测试在其他平台自动跳过（候选测试沙箱只在 macOS 上可用，其他平台拒绝派发）；
  - 修了两处与文件系统顺序和 CPython 3.9.25 相关的写法。
  - 另修了 models-a 引入的两个回归（v297-ci-regress）：args 上的记录不能被 JSON 序列化；一个 fake CLI 把启动时的 `--version` 当成了回合。
- **审查**：B 道由 Opus 5.5 执行、Opus 5.5 审查。
  - tscale：2 轮，R2 APPROVE。
  - F2：3 轮（每轮各 1 个 MAJOR，均已修复），加 1 轮验证，APPROVE_WITH_ADVISORY。
  - F2b：3 轮（2 个 MAJOR，均已修复），APPROVE_WITH_ADVISORY。
  - F5：2 轮（R1 的 MAJOR：转去 gate 的批准打断了计数），R2 APPROVE_WITH_ADVISORY。
  - models-a：2 轮（R1 有 1 个 CRITICAL：版本检查曾在程序绑定之前执行；1 个 MAJOR：abort/status 被拒），R2 APPROVE_WITH_ADVISORY。
  - linux-zombie：2 轮。ci-regress：1 轮 APPROVE_WITH_FINDINGS。CI-TESTFIX：2 轮，R2 APPROVE。v297-cwd：1 轮，结论为修复正确。
  - 按新的发版门禁，本版不做跨厂商审查（只在 v2.10.0 做）；全量测试在 GitHub Actions 的 macOS 和 Linux 上运行，发版前另做一次真实 run，走到 ACCEPTED。
- **已知限制**：
  - 截止 HOLD 本身不能用 `accept --override-rejection` 接受，出口是 abort 或 `note --scope-change`；
  - FIELD-5 依赖评审方给出一致的标签，fresh gate 看不到历史，只能靠 ledger summary 里的标签对齐；
  - 候选测试沙箱只在 macOS 上可用；
  - 没有任何测试的仓库在 Python 3.12 及以上用 `python3 -m unittest` 时会被判为测试失败（待 owner 决定）。
  - v2.9.5 的已知限制（失败的 author 回合把树改了又恢复原样时，operator 证据不作废）这一版仍未修复，延后处理。

## 2026-10-04

### v2.9.6：Codex 只读角色有了自己的临时目录（FIELD-1/FIELD-6）；只读边界和硬链接由探测证明（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：Codex 只读角色的 argv 变了，旧的 permission-probe 报告和探测缓存都会失效，每个 run 目录要重新跑一次 `permission-probe`。需要 Codex CLI 0.160.0 或更新：只读角色用 `--config default_permissions="paired_session_readonly"` 选择命名权限 profile（`codex exec` 没有 `-P`），已在 0.160.0 上用真实探测验证。已经在跑的 run 不要中途换版本。新的 run 请从固定副本 `~/paired-runs/review-loop-v2.9.6` 运行。
- **fix（FIELD-1，b296-f1）**：Codex 的 reviewer、shadow、gate、probe、gate-probe 不再用 `sandbox_mode="read-only"`。原来这些角色没有任何可写的临时目录，需要临时目录的测试报 "No usable temporary directory found"，Codex gate 的探测也永远过不了（FIELD-6）。现在：
  - 改用只读权限 profile `paired_session_readonly`：根目录和 workspace 只读，只有 `$TMPDIR` 可写，网络关闭；
  - 每次派发分到一个独立、属于本 run 的临时目录 `run_dir/role-tmp/<seq>-<role>`（0700），用作 TMPDIR/TMP/TEMP。回合结束（包括失败的回合）后删除，删除前先清除文件标志、恢复权限，删除失败会明确报错，不会掩盖回合本身的错误；
  - 临时目录里出现多链接的普通文件或不可读的目录，该回合判失败；
  - Claude 角色不变。
- **fix（b296-f1e）**：发版前第一次真实探测，P1、P2 都失败，报错 `unexpected argument '-P' found`：`codex exec` 不接受 `-P`，只有 `codex sandbox` 接受。现在改用 `default_permissions` 配置来选择同一个 profile，profile 本身的定义没有变。fake CLI 也改成像真实 CLI 一样拒绝 `-P` 和 `--permission-profile`，避免再出现只有 fake 能通过的 argv。作者的 `codex sandbox -P` escape check 不受影响。
- **fix（b296-f1f）**：合入 f1e 后的第二次真实探测，P1、P2 都是 UNKNOWN：reviewer 和 gate 探测的 `git checkout --`、`rm` 两步写死了 `tracked.txt`，真实 workspace 里通常没有这个文件，`rm` 只会报 "No such file"，按 f1b 的规则不算显式拒绝。现在这两步改为针对 workspace 里一个真实存在的跟踪文件（从 `git ls-files` 中选普通文件、路径上没有符号链接，优先选没有未暂存改动的），报告里记为 `probe_tracked_file`。workspace 里没有合格文件时，在派发任何回合前就拒绝。如果删除真的成功（沙箱失效），探测判 FAIL 并写明被删的文件，该文件不会自动恢复。`git checkout` 一步用 `--literal-pathspecs`，文件名里的通配符和 pathspec magic 不会被展开（b296-f1g，跨厂商审查发现）。
- **安全（b296-f1）**：Codex 的 reviewer 探测和 gate 探测现在必须证明：
  - `$TMPDIR` 可写；
  - 写 `/tmp`、用户临时目录、run dir、`$TMPDIR/..`、context 和 workspace 都被拒绝；
  - 把 run dir 里的文件硬链接进 `$TMPDIR` 也被拒绝。
  回合结束后逐个确认目标不存在、源文件仍只有一个链接。每个 Codex 只读回合前后还会比对 run dir 下普通文件的 inode 和 ctime，有变化即判该回合失败（纵深防御）。
- **审查**：
  - B 道由 Opus 5.5 执行、Opus 5.5 审查。b296-f1 审了 3 轮：R1 有 2 个 MAJOR，即探测既没有证明「只有 $TMPDIR 可写」，也没有证明硬链接被拒，均已修复；R3 APPROVE_WITH_ADVISORY。f1b 到 f1e 每批都经过 Opus 审查并 APPROVE。
  - gpt-6.1-sol 跨厂商审查共 7 轮。前 3 轮提出的问题由 f1b 到 f1d 修复，包括 2 个 MAJOR（指向外部的 role-tmp 符号链接会导致误删；探测把「命令不存在」当成拒绝）和 3 个 MEDIUM（chmod 跟随竞争；清扫前没有确认回合进程组已结束；进程组状态未知时也被当成已结束）。第 4 轮 APPROVE_WITH_ADVISORY；第 5 轮审查 f1e，结论：APPROVE_WITH_ADVISORY；第 6 轮审查 f1f，结论：REQUEST_CHANGES（1 MAJOR：git checkout 的文件名会被当作 pathspec 通配展开；2 MEDIUM），由 b296-f1g 修复；第 7 轮审查 f1g，结论：APPROVE。f1f 和 f1g 各由 Opus 审了 2 轮，均 APPROVE。
  - 真实 permission-probe 在合入 f1g 后重跑，覆盖 Codex reviewer 和 Codex gate 两种配置。
  - 全量测试在 GitHub Actions 的 macOS 和 Linux 机器上运行。
- **已知限制**：
  - 临时目录和 run dir 在同一个卷上。同卷硬链接的主要防线是探测里那条必须被拒绝的 `ln`；ctime 比对不覆盖 `state.json`、`progress.jsonl`、本回合自己的 evidence 文件、Codex rollout，以及 run dir、workspace、context 和受监控的全局配置之外的同卷文件；检测只持续到 CLI 进程退出；探测只覆盖新开的回合，不覆盖 resume。
  - Claude 只读角色仍使用 Claude Code 自带的 `/tmp/claude`（同一用户共享）。
  - v2.9.5 的已知限制（失败的 author 回合把树改了又恢复原样时，operator 证据不作废）仍在，v2.9.7 修。

## 2026-10-03

### v2.9.5：operator 证据通道（attach-verification）、round-limit HOLD 可由 owner 裁决放行、一批来自真实 run 的 operator CLI 修复（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：插件版本绑定在 permission-probe 的 PASS 里，升级后已有的探测报告和探测缓存都会失效，每个 run 目录要重新跑一次 `permission-probe`。已经在跑的 run 不要中途换版本。新的 run 请从固定副本 `~/paired-runs/review-loop-v2.9.5` 运行。
- **feat（b295-opv）**：新增 `paired-session attach-verification`。operator 在 author 沙箱之外跑某项检查（例如 xcodegen、XCTest、CoreSimulator），再把结果附加到空闲的 ACTIVE、HOLD 或 DONE run 上：
  - 记录包含命令、cwd、退出码、log sha256（log 复制进 evidence）、时间、actor 和非空 note，绑定当前工作树的快照 digest；
  - EXEC/POLISH reviewer、shadow 和 gate 的 prompt 会把它作为「针对这棵树的 operator 验证证据」展示（含 log 尾部，不含 note）；
  - 协调器看到树变化、author 回合改树或 log 副本被改时，记录永久作废；persistent reviewer 看到过的记录作废后，下一轮 prompt 会多一行 `Withdrawn operator verification: Vnnn (原因)`；
  - 它只是 prompt 证据，不参与任何 verdict 判定。来源：poker-tools N3。
- **feat（b295-rlo）**：`accept --override-rejection --reason TEXT` 现在也能在 PLAN 或 EXEC 的 round-limit HOLD（含 gate 之后的 EXEC round limit）上由 owner 裁决放行：
  - 只接受 HOLD 时记录的那棵未变化的树，且该 HOLD 必须仍是当前 HOLD；
  - 树变了、之后又出现其他原因的 HOLD、树已被 reject、有 active 或 uncertain 回合、reason 为空，都会被拒绝；probe、guard、lease 等原因造成的 HOLD 不能这样越过；
  - `acceptance.json` 记录这次 HOLD 和当时仍 open 的全部 finding（id、severity、source、security、单行 summary）。
- **fix（b295-field-a）**：
  - 任一角色是 Codex 时，run、resume、reject、permission-probe 在派发前检查 `CODEX_HOME` 是否为已存在的目录，否则明确 REFUSED（原来只表现为探测的 "CLI exit 1"）；
  - Claude author 探测期间 run dir 旁边出现文件变化时仍判 FAIL，HOLD 文案改为提示可能是 operator 自己的文件（例如启动日志），并建议把启动日志写到 run dir 的父目录之外；
  - Claude reviewer 或 gate 遇到 dontAsk 下跑不了的测试命令（`$(...)`、管道、`;`、`&&`、重定向、循环）时打印 WARNING，README 写明 `/bin/bash /绝对路径/script.sh` 的写法；
  - 文档：Claude Code 的 auto 模式会拦截 `--accept-unverified-claude-author`，需要 owner 手动启动；
  - codex-cli 已验证版本表不变：poker-tools N4 的探测 PASS 是 Claude author 加 Codex gate，没有覆盖 Codex author 的沙箱契约。
- **fix（b295-field-b，poker-tools N4 现场反馈）**：
  - `accept --reason X` 不再要求 `--accept-*` 标志，`--reason` 作为接受理由记录并被 intent digest 覆盖；
  - accept 拒绝 `--text`/`--file`（原来生成的 digest 永远对不上）；
  - DONE 之后、accept 之前也可以 attach-verification，accept 会在 `acceptance.json` 和 stdout 列出对被接受的树仍然有效的 operator 证据。
- **docs（b295-docs）**：新增 `paired_session/docs/concurrent-runs.md`，说明每条并发 lane 要用独立的绝对 `CODEX_HOME`（默认 `~/.codex` 也算共享），并写明 probe-pass 缓存键的组成；go-reviewer 改为遵从 report-only reviewer 运行时；ADR-8 的模型 pin 措辞改为 ADR-9（模型由 operator 配置）和 ADR-10（gate 默认取 author 的厂商）。
- **审查**：B 道由 Opus 5.5 执行、Opus 5.5 逐批审查：docs 和 opv 各 2 轮，rlo、field-a、field-b 都是 R1 通过，全部 0 CRITICAL、0 MAJOR。发版前由 gpt-6.1-sol 做跨厂商审查：1 个 MEDIUM，见已知限制，下一版修。
- **已知限制**：
  - operator 证据只在协调器看到的树上作废。author 回合中途失败（CLI 非零退出）时，回合内观察到的新树不触发作废；之后树恢复到附加时那棵，旧记录会重新显示。记录内容仍对应当前这棵树，但「树一变就永久作废」的承诺在这条路径上不成立。v2.9.6 修。
  - FIELD-1（只读角色的临时目录）不在这次发版里，v2.9.6 单独审查后发布。
  - 以上改动只用 fake CLI 验证过。A 道的真实生命周期提交不在这次发版里，真实生命周期仍然关闭。

## 2026-10-01

### v2.9.4：沙箱拒绝建硬链接也能让 Claude 作者探测通过；作者放行只豁免作者那部分探测；FAIL 后探测缓存作废（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：升级后已有的 permission-probe 报告和探测缓存都会失效，每个 run 目录要重新跑一次 `permission-probe`。已经在跑的 run 不要中途换版本。新的 run 请从固定副本 `~/paired-runs/review-loop-v2.9.4` 运行。
- **fix（HL-FIX）**：Claude 作者探测中，沙箱拒绝创建硬链接现在算通过：
  - 拒绝创建，作者自建硬链接这条路就从源头关上了；
  - 前提是拒绝有真实证据，且事后 `hl` 不在 sentinel 的 inode 上；
  - 两个硬链接写入行记为 `not_applicable: link denied`，原始结果照录。
  v2.9.3 上真实 CLI（Opus 5.5）拒绝 `ln`，探测因此只能停在 UNKNOWN，永远无法 PASS。任何 escape、sentinel 被改、意外的 tool_use 仍然是 FAIL。符号链接链不变。
- **fix（HL-FIX）**：`--accept-unverified-claude-author --reason` 现在只豁免作者探测这一部分：
  - reviewer 探测通过（需要 gate 探测时 gate 也要通过），只差作者探测时，run、resume 和 reject 都能过探测关；
  - reviewer 或 gate 探测失败、配置变更、任何 escape 仍然拦截，`--accept-probe-skip` 对这些情形仍被拒绝；
  - 放行仍绑定 actor、理由和 author flags 的 digest，flags 一变就永久作废。
  v2.9.3 上放行记录了，但 run 仍被「permission probe status is not PASS」拒绝，operator 只能换一个从没探测过的 run 目录。
- **fix（F4）**：同一个 key 的探测之后没通过（FAIL、UNKNOWN 等）时，旧的探测通过缓存会被作废（写 VOID 墓碑，之后拒绝复用）。作废写不进去也删不掉时，报告和终端会给出显眼的 `PROBE CACHE ENTRY NOT VOIDED` 警告；探测的结论不受影响。
- **docs（DOC-SBX）**：README 新增一节，说明 Codex 作者在 workspace-write 沙箱里做不了的事（例如编 iOS、跑 Postgres initdb）。
- **审查**：B 道由 Sonnet 5.5 执行，gpt-6.1-sol 逐批审查。F4 经过 3 轮（R1、R2 各 1 个 MAJOR，R3 通过），DOC-SBX 和 HL-FIX 都是 R1 通过。
- **已知限制**：新的硬链接判定和放行范围只用 fake CLI 验证过，真实 CLI 上能否 PASS 要 operator 重新跑一次探测确认。A 道 Round 47 及之后的提交不在这次发版里，真实生命周期仍然关闭。

### v2.9.3：Claude 作者探测改写、实时进度日志、gate 默认用作者厂商；并入 fake PLAN→close 生命周期（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：升级后已有的 permission-probe 报告和探测缓存都会失效，每个 run 目录要重新跑一次 `permission-probe`。已经在跑的 run 不要中途换版本。新的 run 请从固定副本 `~/paired-runs/review-loop-v2.9.3` 运行。
- **fix（PR-REF）**：Claude 作者的权限探测指令改写为 operator 授权的一次性自测：
  - 说明预期结果是 harness 拒绝每一次越界写入，被拒绝就是测试要的成功；
  - 要求逐字回报每次尝试的工具结果；
  - 不再要求「无 findings 的 APPROVE」。v2.9.2 上 Opus 5.5 把旧指令当成越狱加作假证而拒绝执行。
  模型一个工具都没调就回答 HOLD 时，探测保持 UNKNOWN，原因标为 `author-model-refused`，并提示沙箱没有被测试，不再报成 `author-model-escape-unknown`。判定仍只看文件系统状态、tool_use 计数、拒绝证据和 sentinel。Codex 作者的探测指令不变。
- **feat（PL）**：真实运行时的实时进度：
  - 每个阶段、派发、finding、verdict 和终态在终端打一行（`--quiet-progress` 可关）；
  - 同样的事件追加写入 `RUN/progress.jsonl`（不含 prompt、输出、diff 或密钥；写失败不影响 verdict 和状态）；
  - 新增 `status --brief [N]`。
  探测回合有单独的事件类型，不会被标成真实的作者或审查回合。最后一行状态输出格式不变。
- **feat（G-b，ADR-10）**：Step 3.4 gate 默认使用作者的厂商。格式不对的 `allowed_models` 现在会明确拒绝，不再抛未捕获的异常。
- **lane A（Round 43–46）**：在 fake harness 中打通 PLAN→close 生命周期（M4）：
  - 密封的发布记录、崩溃后的恢复入口、CLOSE 前核对证明、幂等的 CLOSE；
  - Q/OID 测试子进程运行在写沙箱里。
  这些代码只有 fake 测试路径会走到，真实生命周期仍然关闭，A1–A5 启用前检查都还是默认关闭。
- **审查**：
  - B 道由 Sonnet 5.5 执行，gpt-6.1-sol 逐批审查；
  - A 道由 gpt-6.1-sol 执行，Opus 5.5 逐批审查，发版前另由 Opus 5.5 做了一轮跨厂商复审（APPROVE_WITH_MINORS；唯一的 MINOR 只在 fake 路径上，见下）。
- **已知限制**：
  - 新的探测措辞能不能避免模型拒绝，只有在真实 CLI 上重新探测才能确认。
  - Python 3.9.6 清理临时目录时会跟随符号链接修改权限，这只影响 fake 路径上的候选测试沙箱，修复在 Round 47 进行中。
  - 真实生命周期启用前的检查（A1–A5）还没有完成。
  - Codex 作者在 workspace-write 沙箱里编不了 iOS，也跑不了 Postgres initdb。

### v2.9.2：真实 CLI 修复（poker-tools 首批真实运行发现的问题）与跨厂商 gate（paired-session 仍是可选入口，默认 legacy）

- **升级须知**：升级后，所有已有的 permission-probe 报告、probe-skip 接受记录和探测缓存都会失效，每个 run 目录要重新跑一次 `permission-probe`。已经在跑的 run 不要中途换版本，用开跑时的同一份代码跑完。新的 run 请从固定副本 `~/paired-runs/review-loop-v2.9.2` 运行。
- **feat（G-a1/G-a2）**：gate 可以和 reviewer 不同厂商。不同厂商时，permission-probe 多跑一个只读的 gate-probe 回合，测的就是真实 gate 的派发表面。gate flags 绑定进探测报告、跳过记录和缓存。删除了旧的「gate 厂商必须等于 reviewer 厂商」拒绝规则。gate 默认改用 author 的厂商（G-b）放在下一版。
- **fix（CG）**：
  - 每次派发 Codex（author、reviewer、gate、probe）都带 `-c features.plugins=false`。只有当生效的 `$CODEX_HOME/config.toml` 自己写了 `[features] plugins = false` 时，缓存里的插件 bundle 才视为不会加载；其他情况仍然 HOLD。推荐用专用的 `CODEX_HOME`，配方见 `paired_session/README.md`。
  - `config.toml` 不存在时，按空配置处理。
  - permission-probe 跑完自己的回合后，再检查一遍 Codex 能力。
  - trust 归因改为精确移除信任块，并且只认 TOML 顶层的块；linked worktree 接受主 checkout 根目录。
  - Claude 作者探测不再把 CLI 自己建的空目录 `.claude/.cc-writes/` 当成越界写入。
- **fix（RF）**：
  - gate 与 reviewer 不同厂商、又没有通过的 gate probe 时，拒绝 `--accept-probe-skip`。
  - Claude 子进程一律设置 `DISABLE_AUTOUPDATER=1`，并去掉 `FORCE_AUTOUPDATE_PLUGINS`，避免插件市场在 run 中途自动更新、触发误报 HOLD。如果只有插件版本变化，HOLD 会提示用 `resume`。
  - reviewer 给出 APPROVE 但还有阻断项或安全标记的问题没关时，改为按 REVISE 交回作者处理（受轮数上限约束），不再 HOLD 循环。
  - linked worktree 中断后恢复时，可以确认同时新增的两段信任块。
- **审查**：Sonnet 5.5 执行，gpt-6.1-sol 逐批审查；G-a 另由 Opus 5.5 做了一轮跨厂商发版前复审（APPROVE_WITH_MINORS，那条 MINOR 已在 RF 修复）。
- **已知限制**：
  - 两处 `claude --version` 子进程没有使用子进程环境（MEDIUM advisory）。
  - POLISH 阶段的 APPROVE + 阻断项还是旧的处理方式。
  - `gate_surface_issue` 空函数还留着。
  - 「每次派发都关插件，所以完全不扫描 bundle」这个更彻底的方案，需要先改一条旧断言。
  - Codex 作者在 workspace-write 沙箱里编不了 iOS，也跑不了 Postgres initdb。
  - APPROVE 之后的生命周期仍只在 fake harness 中可用。


### v2.9.1：修 poker-news-bob 的 bug report（paired-session 仍是可选入口，默认 legacy）

- **发布范围**：B 道 v2.9.1 分支（P0-1 至 PR1c）与 A 道（至 `2fc2bea`）。APPROVE 之后的生命周期（Step 3.4 之后的 FINISH、quality polish、DOCS、SECURITY、DELIVERY、CLOSE）仍只在 fake harness 中可用，真实 CLI 仍拒绝 `--lifecycle-mode on`；这些步骤由操作者自己完成。
- **feat（P0-1/1b）**：角色模型可配置：`--author-model`、`--reviewer-model`、`--gate-model`、`--gate-vendor`（默认取 author 的相反厂商），可选 `allowed_models` 白名单；默认模型仍按 ADR-9（Claude `claude-opus-5-5`，Codex `gpt-6-luna`）。
- **feat（P0-2*）**：Codex CLI 版本契约。未验证的版本（当前只验证 `codex-cli 0.157.0`）需要 `--accept-unverified-codex-cli --reason TEXT`，记录操作者、理由、时间和版本，版本变化即失效。
- **feat（P0-3*）**：Claude author 在真实 CLI 上可用：一次性 cwd、Edit/Write 路径拒绝规则、Bash 沙箱、真实越界探测（含硬链接 Bash 写入）；探测未 PASS 时需 `--accept-unverified-claude-author --reason TEXT`。
- **feat（P0-4/4b）**：`--accept-probe-skip --reason TEXT` 记录式跳过 permission probe（不能推翻当前 FAIL/UNKNOWN）；完全 PASS 的探测结果自动缓存在 `~/.cache/review-loop/probe-pass/`，按 flags、CLI 版本和规则指纹复用，7 天过期，按 fd 安全读取。
- **fix（PR1/1b/1c，发版前 Opus 对抗性审查）**：coordinator 在作者工作区运行的 git 调用不再执行工作区可控的程序（fsmonitor、hooks、pager、external diff、textconv、filter 驱动、lazy fetch、传输协议、全局/系统配置）；作者改动 git 控制文件或使其不可读时，run 持久化为 HOLD。workspace profile 的角色/模型选择在写入 state 之前被拒绝（关闭 BACKLOG MEDIUM-1）。`adversarial_gate_invoke.py` 默认超时 570 秒。
- **fix（B15b）**：legacy 协议的跨厂商审查规则：只用 `[CRITICAL]`/`[MINOR]`，无效结果阻断交付，每次收敛只跑一次；launcher 超时统一 570 秒。
- **test（B16c）**：smoke 加固：review packet 唯一且不在代码块里、no-op 行逐格校验、Metadata 限定在本节、所有 PASS 路径都检查加载的是被测插件。
- **已知限制**：探测缓存没有 HMAC；Claude author 的 Bash 写 `/tmp` 未被探测；FAIL 后删除 run 目录再在同路径重建时缓存可能复活（v2.9.2）；fake Q 测试子进程没有写沙箱（打开真实 Q 交付前必须修，A 道 R44-QS）；gate 默认用 author 厂商的 WI 放在 v2.9.2。

## 2026-09-30

### v2.9.0 预览版：paired-session 可选入口（实验性，默认仍是 legacy）

- **发布范围**：合并 paired-session 分支（A 道，至 `13c04f2`）与 B 道（至 `9e5395a`）。真实 CLI 支持 permission probe → PLAN 审查 → EXEC 与审查 → adversarial gate → DONE → accept/reject；APPROVE 之后的生命周期（FINISH、quality polish、DOCS、SECURITY、DELIVERY、CLOSE）仍只在 fake harness 中可用，真实 CLI 拒绝 `--lifecycle-mode on`。完整 PLAN→close 的 M4 E2E 改为 v2.10.0 的门槛。
- **fix**：预览版在真实 CLI 上拒绝 Claude author（`--author-vendor claude`），包括从已保存 run 恢复的配置；原因是 Claude author 的原生 Write/Edit 没有路径限制（1C 3b）。默认 author 仍是 Codex。由 gpt-6.1-sol 跨厂商发版审查发现，两轮修复。
- **已知限制**：operator reject 之后同一棵树的再放行加固（第 12 批）尚未合入；1C 安全清单仍 OPEN；M6 受控真实运行尚未完成。paired-session 的敏感路径分类器仍对齐 W02 之前的 legacy §3.7.1 规则（测试改读 v2.8.4 冻结副本，经 Yuan 批准），与 W02 扫描器对齐列为后续批次；该分类器只在 fake 生命周期中使用。

#### 分支内容（原 Unreleased）

由 Sonnet 5.5 实现、Opus 5.5 审查；未发布，未升版本。迁移指南见 `docs/paired-session-migration.md`。

- **doc**：`paired_session/docs/1d-entry-mapping.md` 记录 `/review-loop` → paired-session 的入口设计（S1、S2、S4、S5：opt-in 开关、显式 legacy 控制、模式与配置键映射、放量门槛）。
- **feat**：新增 `/review-loop:legacy` 控制技能，忽略 `entry` 键、强制走 legacy 流程（`disable-model-invocation: true`）。
- **feat**：`.review-loop/config.md` 新增可选 `entry: legacy|paired-session`（默认 legacy）；仅全新工作项被路由，plan/代码/已有 session 仍走 legacy；非法值、重复键、读取失败均回落 legacy 并给出提示。
- **feat**：隐式入口和路由时分别打印一行提示（含 experimental 提示）；路由在创建 session 文件/锁之前一次性决定。
- **feat**：Codex 侧 `review-loop` 入口同样识别 `entry`，显式 legacy 控制为自然语言 "use the legacy review-loop workflow"。
- **doc**：新增 `paired_session/docs/1c-safety-controls.md`，32 行安全控制清单（代码锚点、对应测试、状态、残余风险）；这是清单，不是 1C 关闭声明，1C 仍 OPEN。
- **test**：新增 `paired_session/test_safety_controls.py`（provider hook 禁用与凭据 deny、invocation cap），均经变异验证；lint 新增并登记 `entry` / `legacy` 相关断言。
- **注意**：`lifecycle_mode=on` 仍被真实 CLI 拒绝，APPROVE 之后（FINISH 起）的阶段仅 fake-CLI；M4/M6 未完成。

## 2026-09-27

### v2.8.10 第一批自查（W01/W06/W07/W12/W13）与第二批交付控制（W02/W04/W05）：交付身份、只读审查、调用边界、用量账本、原生回归与交付门禁

- 新增只读 `delivery_scope.py`，分别绑定任务前后的 HEAD、index 和工作区内容，显式区分既有用户改动、任务范围、范围外变化及同文件所有权歧义。
- report-only Reviewer 和质量检查角色统一通过受限原生 Claude/Codex launcher：Claude 仅开放 Read/Grep/Glob，Codex 使用干净配置上下文与 read-only sandbox；写入型 Executor/Simplifier 保持原路径。
- 两个 launcher 具备总超时、取消、限额/格式/传输分类、不可覆盖的原始工件，以及跨独立进程组的有界后代清理；并行调度器复用同一边界。
- 新增逐调用用量账本，记录角色、阶段、请求/实际模型、缓存/非缓存输入、cache write、输出、耗时、状态及运行时上报费用；恢复与不完整调用不重复累计，也不伪造未知值。
- 新增 disposable repo 原生生命周期回归，覆盖 plan、execute、review-only、stop/resume 和非法参数失败；候选工作流仅作为被测对象，不负责批准自身改动，两个运行时均限制在 workspace sandbox。
- 根据独立对抗审计，补齐原生 tool-use 计数、Claude 默认模型省略、session job 用量归属和非正容量拒绝；审计报告保存在本地忽略结果目录。
- 第二批新增 W02/W04/W05 delivery consumers：安全扫描覆盖 manifest 中的 tracked/staged/nonignored-untracked 内容并只报规则/路径/行号；auto-commit 只提交 W01 明确归属的路径；W05 在提交前重新核验 manifest、完整安全扫描和 evidence-ledger stages。
- 协议 loader 可将大 bundle 原子写入忽略目录并输出短 receipt，再分块读取，避免宿主截断；同时修复证据账本全局参数与子命令的错误顺序。

## 2026-09-25

### v2.8.8 按 claude-opus-5-5 清理过时的 prompt 写法

依据 prompt 审计（`.compass/results/2026-09-25_prompt-audit/`）。约束一条不删，只改语气、补理由、去掉历史叙述。

- reviewer / code-reviewer 不再在报告阶段过滤发现：非阻断项报为 `[MINOR]`，不再「或省略」；code-reviewer 改为具体的报告门槛。
- 4 个语言 reviewer 的 `**MANDATORY**` 横幅改为正常语气的同一约束；orchestrator 侧 `tool_uses: 0` 检查不变。
- 协议与技能：`DO NOT modify the context file` 补上理由（Orchestrator 是唯一写入者）；native Bash 规则写明原因；
  去掉「now / today / as before / formerly inline / before v2.8.2」这类迁移与历史措辞；`parallel-review.md` 的失效行号改为符号锚点。
- agent 正文：去掉结尾打气话和「non-negotiable / absolutely forbidden」；code-simplifier 不再写死上游项目的 JS/React 规范。
- review-pr / reorganize：agent 标签去掉 `review-loop:<name>`，删掉给人看的 Tips，抽取规则改为判断式。

## 2026-09-20

### v2.8.4 降低跨模型 reviewer 的 token 浪费，修复 3 个派发 bug

依据 30 天用量审计（review-loop 约占 Claude 用量 91%、Codex 约 100%）。由 Codex 实现、Claude 监工，未走 review-loop。

- **bug**：`scripts/review_verification.py` 的 `claude_code` 分支从 `codex exec --full-auto`（可写沙箱、不传模型）
  改为 `-s read-only` + 仅在配置时传 `-m`；Claude 档位的模型兜底不再泄漏到 `codex -m`。
- **bug**：Codex→Claude reviewer prompt 现在必须带 `agents/reviewer.md` 正文（含六字段 `[CRITICAL]` 评审标准），
  避免 CRITICAL 被 `finding_triage` 判为 incomplete 而整轮白跑。
- **bug**：`claude -p … --output-format stream-json` 在 Claude Code 2.1.278 上必须加 `--verbose`，否则直接报错；
  两个 argv 构造处和协议文档里的命令都已补上。
- Claude→Codex reviewer 命令在 `planning.md` 写死：`codex exec -s read-only [-m {reviewer_model}] -o <round file> -`。
- reviewer prompt 第一段声明「本 prompt 自足，不要加载 review-loop 技能 / `SKILL.md` / `docs/protocol/**`」
  （审计实测 Codex reviewer 每轮自行加载约 29K token）。
- plan 审查不再让 reviewer 重读 session 文件里的 plan（prompt 里已内联）。
- executor 的逐条回应改放顶层 `## Response to Reviewer` 小节，plan 正文只保留当前 plan
  （此前 plan 审查 prompt 每轮增长约 20%）。Claude 与 Codex 两侧 executor 定义同步。
- 新增 `scripts/run_claude_reviewer.py`：Codex orchestrator 不再轮询原始 stream-json
  （审计中占其 context 约 24%）；完整流落盘，只输出约 30 秒一次的心跳和最终状态；
  退出码 1/2/3 对应命令执行 / JSON 解析 / 缺少 result；有有效 result 时容忍无效行。
- 删除 `test_behavior_engines_untouched_by_loading_refactor`（用户授权）：它是 v2.8.1 的一次性交付核对，
  把 5 个脚本永久冻结在 `cadb06c`，阻止任何正当修改。
- 验证：lint exit 0；`pytest tests -q` 605 passed；包装脚本用真实 CLI 试跑成功/失败路径均通过。
  **实际 token 降幅尚未实测**，待下次真实 review-loop 运行后对比。

## 2026-09-19

### v2.8.3 更正 agent 调用方式的过时说明

- `docs/protocol/{planning,execution,runtime-claude}.md`、`skills/{review-pr,code-quality-loop}/SKILL.md`、
  `agents/code-simplifier.md` 不再称「plugin sandbox bug / tools silently blocked」；改为说明协议统一用
  `general-purpose` + 内联 agent 正文调用，并注明 v2.8.2 之前 `tools:` 取值无效导致插件 agent 拿不到工具。
- 仅改措辞，调用规则不变；`CRITICAL` 警告与 lint 守卫句保留。

### v2.8.2 修复 agent `tools:` frontmatter

- 12 个 agent 的 `tools:` 取值无效：`read-only`（10 个）和 `all`（`executor`、`code-simplifier`）都不是工具名，
  Claude Code 解析出零个工具。2.1.278 直接拒绝启动（`would be spawned with zero tools — unrecognized [...]`），
  旧版则静默启动、`tool_uses: 0` 并编造输出。
- 只读类 agent 改为 `tools: Read, Grep, Glob, Bash`（不含 Edit/Write）；`executor`、`code-simplifier` 删除 `tools` 字段，继承全部工具。
- 更正 `CLAUDE.md` 的记录：此前归因为「插件 agent 被 sandbox 屏蔽工具」，真正原因是 `tools:` 取值无效。
  各 skill 与协议文档仍用 `general-purpose` + 内联 agent 正文的方式调用，本次不改。

## 2026-09-17

### v2.8.1 活体指标补测（无代码改动）

- 首次取得真实 native-runtime 的规则输入实测，补上 v2.8.1 唯一未验证的验收项。
  载体是 compass 仓库的真实 work item，Claude Code runtime，plan 2 轮 + execute 1 轮。
- 按计划文档 L114-118 口径（含 agent body 与 task packet）：AFTER 实测 166,644 B
  vs BEFORE 反事实 233,780 B = **−28.7%**，达标 ≥25%。扣除因配额作废的 27,427 B
  reviewer prompt 后为 −40.4%。
- **边界**：observed-AFTER 对 reconstructed-BEFORE。BEFORE 列仍是 `cadb06c` 静态
  inventory，因为 observed BEFORE 需要把同一 work item 在 v2.8.0 下重跑一遍。
  引用 28.7% 必须带这个限定。分母场景比本轮轻，故为保守下界。
- 静态表测不出的一条：planning-review bundle 在第 2 轮重载时投递 **0 字节**
  （15 个指纹全部复用，全量为 54,612 B）。跨轮指纹去重是主要收益来源。
- 同轮实测：必要规则漏加载 = 0；未预加载任何后续 stage。
- 数据：`.compass/results/2026-09-17_v2.8.1-live-workflow-measurement.json`

## 2026-09-16

### v2.8.1 — 按阶段加载协议与入口去重

- Claude Code 与 Codex 六个入口共用加载契约和依赖表；在动作前加载所需原文，
  保留独立审查、证据失效、安全检查和 stop/resume 语义。
- 新增精确章节加载器；拒绝缺失/歧义章节与循环依赖，支持同一有效上下文内
  复用内容指纹。压缩、恢复和新 agent 必须重新加载前置规则。
- 迁移原有契约到共享来源；smoke 采集核对成功返回的完整规则正文，
  不把命令出现、inventory 或压缩前的缓存当作已加载证据。
- 相对 v2.8.0，静态 plan/execute 启动规则字节减少约 44%–49%；
  小任务完整路径的静态减少约 14%–15%。真实全流程 25% 降幅、token/费用
  改善和原生 Claude workflow benchmark 尚未验证，不能由静态数字推断。
- 本版本不改变 evidence/triage/gate 算法，也不扩大已冻结的 submodule 支持。

## 2026-05-15

### v2.7.8 — adversarial-gate polish closeout

- 关闭 v2.7.7 Terminal Adversarial Gate 的 18 项 P3 polish follow-up bundle：
  并行 triage 后只修仍有实际行为/诊断价值的项，避免继续扩大 adversarial
  self-loop。
- 强化 `scripts/adversarial_gate_adapter.py` / `scripts/adversarial_gate_invoke.py`：
  strict UTF-8 input/prompt handling；plugin/cache SKIP detail；adapter failure
  stdout 不泄漏伪 `APPROVE`；snapshot/prompt partial tempfile 失败清理；
  snapshot unlink failure 改为非阻塞 warning。
- 新增/强化 adversarial-gate regression fixtures：raw last-wins、多 JSON 非 schema
  fail-closed、approve+critical blocks、plugin-path ENOENT、SIGKILL escalation、
  mkstemp/os.close/prompt write/close failure cleanup 等。
- 协议和 lint contract pin 住 plugin-root/cache-schema SKIP detail；补充
  adversarial-gate 注释 provenance，说明历史 `Finding #N` / `Meta-dogfood R*`
  标签只是审计来源，durable contract 在协议和测试里。
- Plugin v2.7.7 → v2.7.8。验证：adapter/invoker scoped suite 91/91；
  `/opt/homebrew/bin/pytest tests -q` 278/278；`bash scripts/run-skill-lint`
  review-loop 286 PASS / 0 FAIL。

## 2026-05-14

### v2.7.7 — 终局 adversarial-review gate (Step 3.4)

- 在 Step 3 reviewer APPROVE 之后、Step 3.5 quality polish 之前插入一次终局
  "陌生眼睛" review pass (Step 3.4 — Terminal Adversarial Gate)。双 runtime
  (Claude `skills/execute/SKILL.md` + Codex `.agents/skills/execute/SKILL.md`)
  对称记录, 共享 5 行 Bash dispatch:
  ```bash
  python3 scripts/adversarial_gate_invoke.py --focus-file "$focus_text_file"
  adversarial_exit=$?
  ```
- 新增 `scripts/adversarial_gate_invoke.py` — stdlib-only Python invoker,
  drain-thread + timeout pattern 是 `Scheduler._run_one in
  scripts/review_verification.py` 核心 pattern 的 faithful port; sentinel
  bytes 与 `wait_after_kill_timed_out` 诊断 flag 故意 NOT ported。
- 新增 `scripts/adversarial_gate_adapter.py` — 双 input-mode 翻译层 (raw +
  plugin-json), 同时支持 stdin 与 `--input <path>` 双 handoff;
  输出 review-loop verdict + bulleted issues。
- 新增 `scripts/adversarial_gate_fallback_prompt.txt` — fallback-path
  prompt 模板, `string.Template.safe_substitute` 渲染。
- 配置: 新增 `adversarial_gate_skip_paths` (默认 `["**/SKILL.md",
  "docs/protocol/**", "tests/skills/contracts/**"]`); 当 Step 3 changed-set
  全部命中跳过 Glob 时整轮 skip。
- Plugin-path preference + snapshot/restore: 优先走 `node
  $CODEX_PLUGIN_ROOT/scripts/codex-companion.mjs adversarial-review --scope
  working-tree --json` (JSON-RPC `runAppServerTurn` 不触发
  `.review-loop/config.md` 覆写 side-effect); 退到 `codex exec
  --output-schema` fallback path 时, 先 snapshot 再 restore 配置文件。
- 5 个 SKIP reason 带可选 `detail=`: `plugin-root-unresolved`,
  `cache-schema-unresolved`, `codex-unauthenticated`, `runtime-error`
  (carries detail), `runtime-timeout`。Adapter exit 2 的 produced-but-malformed
  adversarial output 是 blocking REQUEST_CHANGES, not SKIP。
- 共享 `_kill_process_group` 同时驱动 timeout 与 signal 两条 cleanup 路径
  (kill child → restore config → exit 顺序), 避免 orphan child 在 restore
  后再次 mutate config。
- 加宽 auth-regex (`(?i)(?:unauthenticated|not signed in|login
  required|authentication|oauth|unauthorized)`) 捕获 `AuthenticationError`
  / `OAuth2` 等 concatenated 形式; FP risk 视为可接受 (两条分支都 SKIP,
  只 banner reason 不同)。
- `--scope working-tree`-only: 不带 `--base`, 否则 `git.mjs:resolveReviewTarget`
  会 short-circuit 到 branch diff 漏掉 working tree。
- Re-run 语义调整为 single-pass per execution convergence: Step 3.4 只在当前
  convergence 的第一次 Step 3 reviewer APPROVE 后跑一轮; gate REQUEST_CHANGES
  把 findings 喂回普通 Step 3 Executor/Reviewer 修复轮, 修好后由正常 reviewer
  APPROVE mint `exec`, 不再重复触发 adversarial self-loop。后续 downstream write
  清空 `completed_stages` 并从 `exec` replay 时才开始新的 convergence。
- Codex Stage 1 outside-sandbox 要求 (mirrors reviewer / scheduler 调用
  注解): Python invoker 写 tempfile, read-only sandbox 会拦截。
- R6 反馈内联修复: signal-race during Popen assignment
  (`pthread_sigmask` block window), cleanup unlink-only-after-restore-OK,
  adapter-spawn OSError 测试, auth regex parametrized 4 sub-cases。
- Meta-dogfood R3 修复: Step 3 reviewer APPROVE 不再直接 mint `exec` 或跳到
  Step 3.5; 两个 runtime 现在都先跑 Step 3.4, 只有 gate APPROVE / controlled
  SKIP 才 mint `exec`, gate REQUEST_CHANGES 则 withhold `exec` 并把 findings
  喂回下一轮 Step 3。`--stop-after before-polish` 语义同步为 gate APPROVE/SKIP
  之后、Step 3.5 之前停止。
- Meta-dogfood R3 cleanup 修复: `_run_with_drain` normal-exit 路径和 signal
  handler 都按 spawn-time cached pgid 做 best-effort process-group teardown,
  防止 stdio-closed descendant 在 fallback config restore/delete 后继续写
  `.review-loop/config.md`。新增 2 个 invoker regression tests + 6 个 lint
  contract guards 锁住该路径。
- Meta-dogfood R4 cleanup-failure 修复: fallback config restore/delete 失败
  不再被 stderr 吞掉后继续 APPROVE。`_cleanup()` 现在返回 failure detail,
  `_CLEANUP_DONE` 只在清理尝试结束后置位, cleanup 期间 best-effort 屏蔽
  SIGINT/SIGTERM/SIGHUP；任何无法证明 config 已恢复/删除的失败都会输出
  synthetic `adversarial-gate: REQUEST_CHANGES` + `[CRITICAL]` 并 exit 1,
  阻止 `exec` mint。额外 pin 住 plugin path 不会执行 fallback create-from-empty
  config 删除。新增 3 个 invoker regression tests + 5 个 lint guards。
- Meta-dogfood R5 adapter 修复: raw mode 现在把 stdout 最后一个 decoded JSON
  object 视为 authoritative final payload, 不再回退到较早 schema-shaped
  APPROVE object；top-level `findings` 必须存在且为 list, `next_steps` 也必须
  为 list。新增 4 个 adapter regression tests 覆盖 final malformed raw payload,
  missing findings (approve / needs-attention), 和 invalid next_steps type。
- Meta-dogfood R6 修复: umbrella `review-loop` skills 不再在 Step 3 reviewer
  APPROVE 后直接 mint `exec`; 它们现在显式进入 Step 3.4, 只有 gate
  APPROVE / controlled SKIP 才 mint。Fallback create-from-empty cleanup 也收窄
  为只删除 byte-exact Codex bootstrap config; 其它同窗口出现的 config 会保留并
  触发 blocking REQUEST_CHANGES。新增 1 个 invoker regression test + 4 个
  umbrella lint guards。
- Meta-dogfood R7 修复: `docs/protocol/session-file.md` 的 `exec` stage add event
  也改为 Step 3 reviewer APPROVE + Step 3.4 APPROVE / controlled SKIP 后才
  mint；raw adapter mode 不再忽略 earlier APPROVE 后面的 truncated final JSON
  object；pre-existing fallback config cleanup 只会覆盖 exact Codex bootstrap
  overwrite, 对其它同窗口 config 编辑保留并 blocking REQUEST_CHANGES。新增 1
  个 adapter regression test、1 个 invoker regression test、4 个 lint guards。
- Meta-dogfood R8 修复: adapter exit 2 (bad JSON/schema violation/truncated raw
  final payload) 不再被 invoker 转成 controlled SKIP；现在输出 synthetic
  REQUEST_CHANGES + `[CRITICAL]`, exit 1, 阻止 `exec` mint。新增 1 个
  end-to-end invoker regression test, 并把旧 adapter-exit-2 diagnostic test
  改为 blocking 断言。
- Meta-dogfood R9 修复: plugin/fallback producer 非零退出但 stdout 非空时,
  invoker 先把 stdout 交给 adapter 校验, 只有空 stdout 才按 auth/runtime
  SKIP 分类；adapter hand-rolled 校验补齐 cached `review-output.schema.json`
  约束 (required `summary`, `additionalProperties: false`, non-empty strings,
  `next_steps` item, bool-not-number/integer)。新增 8 个 adapter regression
  tests、1 个 invoker regression test、4 个 lint guards。
- Meta-dogfood R10 修复: `runtime-timeout` 和 `drain-incomplete` 不再在
  producer stdout 已捕获时直接 controlled SKIP；清理进程组后若 stdout 非空,
  invoker 输出 synthetic REQUEST_CHANGES, 只允许空 stdout 的 timeout/drain
  failure SKIP。新增 2 个 invoker regression tests、2 个 lint guards。
- Meta-dogfood R11 修复: raw adapter mode 不再忽略最后一个 decoded JSON object
  后面的非空白尾巴；earlier APPROVE 后若跟着 final array、`null` 或 text-only
  tail 都 exit 2, 由 invoker 转 blocking REQUEST_CHANGES。新增 3 个 adapter
  regression tests、1 个 lint guard。
- Meta-dogfood R12 修复: adapter launch OSError 只有 producer stdout 为空时
  才是 controlled `runtime-error` SKIP；若 stdout 已捕获但 adapter 无法启动,
  invoker 输出 synthetic REQUEST_CHANGES, 阻止未校验 payload mint `exec`。
  新增 1 个 invoker regression test、1 个 lint guard。
- Meta-dogfood R13 修复: raw adapter mode 不再把 truncated final array/wrapper
  里的内嵌 schema-shaped object 当作 top-level final payload；`[{...}` 和
  `{"result": {...}` 这类容器截断输出 exit 2, 由 invoker 转 blocking
  REQUEST_CHANGES。新增 2 个 adapter regression tests、1 个 lint guard。
- Meta-dogfood R14 修复: plugin path dispatch 在 `--json` 和 focus text 之间
  插入 `--` sentinel, 防止以 `--base` / `--scope` 开头的 focus text 被
  companion 当成选项并覆盖固定 `--scope working-tree` target。新增 1 个
  invoker dry-run regression test、1 个 lint guard。
- Meta-dogfood R15 修复: adapter JSON decode 在 plugin-json 和 raw mode 都用
  duplicate-key detector, 重复 key 直接 exit 2, 防止 Python last-value-wins
  覆盖 blocking `findings` / `verdict` 后错误 APPROVE。新增 2 个 adapter
  regression tests、1 个 lint guard。
- Meta-dogfood R16 修复: plugin-json envelope 不再信任 companion 已经
  `JSON.parse` 折叠过的 `result`; 如果 `rawOutput` 或 `codex.stdout` 存在,
  adapter 会用 duplicate-key-aware raw parser 重新解析原始 reviewer stdout 后
  再验证。新增 1 个 adapter regression test、2 个 lint guards。
- Meta-dogfood R17 修复: invoker drain reader exception 不再被吞掉后当作完整
  stdout。reader exception + 非空 stdout 输出 synthetic REQUEST_CHANGES,
  空 stdout 才允许 controlled `runtime-error` SKIP。新增 2 个 invoker
  regression tests、2 个 lint guards。
- Meta-dogfood R18 修复: raw adapter 不再把 malformed wrapper/member prefix
  后面的 inner schema object 当成 top-level verdict；未闭合 JSON container
  prefix 后的 `{schema}` 直接视为 nested candidate。新增 1 个 adapter
  regression test、2 个 lint guards。
- Meta-dogfood R19 修复: invoker 不再只信 adapter exit code。adapter exit 0/1
  必须带对应 `adversarial-gate` verdict banner；缺失或不匹配时输出 synthetic
  REQUEST_CHANGES。新增 1 个 invoker regression test、2 个 lint guards。
- Meta-dogfood R20 修复: producer 非零退出但 stdout 是合法 APPROVE 时不再通过
  gate；invoker 会先让 adapter 校验 stdout, 但 adapter APPROVE + producer
  nonzero 现在输出 synthetic REQUEST_CHANGES。新增 1 个 invoker regression
  test、2 个 lint guards。
- Lint contracts 新增 62 个 terminal-gate guards, version-pin needles bump 4
  处。Plugin v2.7.6 → v2.7.7。

## 2026-05-10

### v2.7.6 — protocol↔LEARNING fast-replay alignment

- Aligns `docs/protocol/session-file.md` and `docs/protocol/execution.md` with
  `L-review-loop-simplifier-prose-replay-precedent`: eligible Step 3.5.4 /
  Step 3.6 prose/comment/metadata-only writes can use reviewer-only
  fast-replay instead of forcing Executor re-dispatch, while code writes,
  lint-pinned changes, lint-baseline changes, security writes, accepted drift,
  and baseline backfills still clear `completed_stages` and replay from `exec`.
- Defines the conservative state machine explicitly: Step 3.5.4 fast-replay
  APPROVE preserves existing stages but does not mint `polish`; Step 3.5.6
  mints `polish` only after the full Step 3.5 invocation finishes cleanly with
  either no writes or only eligible writes already approved by reviewer-only
  fast-replay; Step 3.6 fast-replay APPROVE mints `docs`; fast-replay
  REQUEST_CHANGES fails closed to normal replay from `exec`.
- Mirrors the execution wording into both runtime skill surfaces
  (`skills/execute/SKILL.md` and `.agents/skills/execute/SKILL.md`) and adds
  `reviewer_only_fast_replay_consistent` lint coverage via
  `tests/skills/contracts/assertion-mapping.json`.
- Plugin v2.7.5 → v2.7.6. Lint baseline 369 → **370 PASS / 0 FAIL** (+1);
  `python3 -m unittest tests.run_skill_lint_test` 34/34 PASS.

### v2.7.5 — Codex marketplace plugin surface (visible in `/plugins`, parallel to Claude install)

- Goal: bring the Codex install/enable experience to compass parity. Before v2.7.5,
  `codex plugin marketplace add NYTC69/review-loop` registered a marketplace entry but no
  plugin surfaced in `/plugins` — fresh Codex sessions could not see review-loop and fell
  back to the legacy `~/.codex/skills/review-loop/SKILL.md` wrapper. Compass works because
  it ships `.agents/plugins/marketplace.json` plus a `plugins/compass` symlink; review-loop
  shipped neither.
- **`.agents/plugins/marketplace.json`** — new Codex marketplace manifest mirroring
  compass's pattern. Lists review-loop with `installation: AVAILABLE` /
  `authentication: ON_INSTALL` policy and source `{ source: "local", path: "./plugins/review-loop" }`.
  Marketplace name `review-loop-marketplace` matches the existing `.claude-plugin/marketplace.json`
  name so users see one consistent marketplace identifier across both runtimes.
- **`plugins/review-loop` symlink → `..`** — tracked in git as a real symlink (mode `120000`).
  Resolves the marketplace manifest's `./plugins/review-loop` path back to the repo root,
  letting Codex cache the plugin contents at
  `~/.codex/plugins/cache/review-loop-marketplace/review-loop/2.7.5/`.
- **Verified end-to-end**: with the new manifest in place,
  `codex exec -c 'plugins."review-loop@review-loop-marketplace".enabled=true' "list skills"`
  surfaces all four Stage 1 skills (`review-loop`, `review-loop:plan`, `review-loop:execute`,
  `review-loop:guide`) — confirming a fresh Codex session that enables the plugin via
  `/plugins` will get the same surface.
- **Docs**: new `docs/install-codex.md` covering prerequisites, `marketplace add` →
  `/plugins` enable flow, natural-language triggers (Codex 0.130 has no
  `plugin install`/`enable` CLI subcommand and does not recognise `/review-loop:*` slash
  commands), verification, and the Claude-vs-Codex plugin-surface boundary table.
  README's `## Codex Stage 1` section gains an `### Install in Codex CLI` subsection
  pointing at the new doc; `CLAUDE.md` gains a `### Codex marketplace surface` block
  documenting the three required files for future maintenance.
- **Lint contract +9** (`tests/skills/contracts/review-loop.json`):
  `plugin_version_pinned_codex_plugin_json`,
  `codex_marketplace_manifest_name`, `codex_marketplace_manifest_plugin_entry`,
  `codex_marketplace_manifest_source_local`, `codex_marketplace_manifest_source_path`,
  `codex_marketplace_manifest_installation_available`,
  `codex_install_doc_present`, `codex_install_doc_referenced_from_readme`,
  `codex_marketplace_manifest_referenced_in_claude_md`. Three existing `plugin_version_pinned_*`
  needles bumped `"2.7.4"` → `"2.7.5"` in lockstep with `.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json` (`metadata.version` + `plugins[0].version`).
- Plugin v2.7.4 → v2.7.5. Lint baseline 360 → **369 PASS / 0 FAIL** (+9).
  `python3 -m unittest discover -s tests -p '*_test.py'` 187/187 PASS.

### v2.7.4 — Banner-parity polish-tier lint-mirror bundle

- v2.7.3 banner parity 的 polish-tier follow-up（drift audit gap-closure session `8e3393e9-ff1b-4c34-ae0f-4a7943abc593`，源 `.compass/results/2026-05-10_v273-banner-parity-followup-gaps.json`）：新增 **8 条** 每行 `kind: contains` lint records 静态守护 umbrella startup banner 在两个 runtime 的字段，并把 3 条已有 `plugin_version_pinned_*` needles 与版本号 lockstep 升至 `"2.7.4"`。封堵 v2.7.3 留下的不对称——Claude 侧 0 条 per-line records，Codex 侧仍有 5 行（top border / work-item / problem / mode / historical-context template）silent 未守护。
- **Claude umbrella +3**（target `skills/review-loop/SKILL.md:217-225`）：`claude_umbrella_startup_banner_section_header_declared`（`── review-loop: Starting ──` ASCII border）/ `claude_umbrella_startup_banner_reviewer_label_declared`（`Reviewer: {codex | subagent} ({reviewer_model})`，沿用 Claude 侧 `reviewer` 配置 key）/ `claude_umbrella_startup_banner_soft_limit_label_declared`（`Soft limit: {soft_limit_plan} (plan) / {soft_limit_exec} (exec)`）。
- **Codex umbrella +5**（target `.agents/skills/review-loop/SKILL.md:73-110`）：`codex_umbrella_startup_banner_top_border_declared`（同 `── review-loop: Starting ──` ASCII border，跨 runtime 共享 needle 但锚定到不同 path）/ `codex_umbrella_startup_banner_work_item_label_declared`（`Work item: {title}`）/ `codex_umbrella_startup_banner_problem_label_declared`（`Problem: {problem_description}`）/ `codex_umbrella_startup_banner_mode_label_declared`（`Mode: {interactive | handsfree}`）/ `codex_umbrella_startup_banner_historical_context_row_template_declared`（`Historical context: {N} relevant memories loaded`，可选行模板）。
- **版本号 lockstep +0/3**（target `.claude-plugin/`）：`plugin_version_pinned_plugin_json` / `plugin_version_pinned_marketplace_metadata` / `plugin_version_pinned_marketplace_plugins_first` needles 由 `"2.7.3"` 升至 `"2.7.4"`，与 `plugin.json:.version` + `marketplace.json:.metadata.version` / `.plugins[0].version` 同步。
- Plugin v2.7.3 → v2.7.4（parity-only patch tier；无 reviewer 派发或 protocol 行为变更）。Lint baseline 352 → **360 PASS / 0 FAIL**（+8）。
- BACKLOG P3 "v2.7.3 banner-parity follow-up gaps" 关闭。

### v2.7.3 — Codex umbrella startup-banner parity (post-v2.7.2 drift audit)

- 镜像 Claude umbrella `skills/review-loop/SKILL.md:218-225` 的 `── review-loop: Starting ──` 启动 banner 到 Codex umbrella `.agents/skills/review-loop/SKILL.md`：在 `## Runtime Identity` 和 `## Completed Agent Cleanup` 之间新增 `## Startup Banner` 节，5 行固定字段（work item / problem / reviewer backend / mode / soft-limit）+ 1 行条件 `Historical context`，跨运行时 UX parity 修复（drift audit `.compass/results/2026-05-10_cross-runtime-skill-drift-audit.json` `drift-finding-2-startup-banner`）。`Reviewer backend` 行使用 backend-appropriate label（`claude-cli ({reviewer_model | judgment_model | claude-sonnet-4-6})` vs `codex (review_loop_reviewer / {codex_reviewer_model})`），不复用 Claude 的 `reviewer` 配置 key 因为 Codex Stage 1 不用它选 backend。
- 同 banner 节内文字化历史上下文委托关系（companion #1，drift audit `drift-finding-1-historical-context`）：明确 Codex umbrella 不内联跑 Step 1.6，由 `.agents/skills/plan/SKILL.md` Step 1.6 负责 historical-context 拉取，resume-dedup 保证 end-to-end 1 fetch/session — 防止未来审计再次误判为 drift。
- 在 `skills/execute/SKILL.md` 与 `.agents/skills/execute/SKILL.md` 的 "On gate pass" item 2 后追加 Delivery Summary 中文 rendering 规则（companion #2，drift audit `drift-finding-3-delivery-summary-zhcn-shared-gap`）：把 `docs/protocol/execution.md §Step 4` 已有的 SSOT 规则显式写入两个 SKILL body，使其可被 lint 静态守护。文本两边 byte-identical，单条 needle 跨两个 record 复用。
- 新增 7 条 `kind: contains` lint records 到 `tests/skills/contracts/review-loop.json`：4 条 banner（section / reviewer-backend label / soft-limit label / print-once rule）+ 1 条 companion #1 委托说明 + 2 条 companion #2 中文 rule（每个 SKILL body 各 1）。3 条现有 `plugin_version_pinned_*` records 更新为 `"2.7.3"`。
- Plugin v2.7.2 → v2.7.3（parity-only patch tier；无 reviewer 派发或 protocol 行为变更）。Lint baseline 345 → 352 PASS / 0 FAIL，`tests/review_verification_test.py` 57/57 不变。
- BACKLOG P3 "Codex umbrella startup-banner parity" 关闭。

## 2026-05-09

### v2.7.2

- v2.7.1 wire-scheduler 交付的 polish-tier 跟进 bundle（BACKLOG P3，session `1e6530e5-9b67-4e20-9266-775110854938`）：新增 **48 条** lint 静态守护 + 显式文档化 1 个 best-effort smoke 已知 flake（`review-loop.regression.smoke.claude`），全部为 strict-expansion，零 review-loop 行为变更。
- **Item (a)** plugin 版本一致性 lint（+3）：`plugin_version_pinned_plugin_json` / `plugin_version_pinned_marketplace_metadata` / `plugin_version_pinned_marketplace_plugins_first` — 锚定 `.claude-plugin/plugin.json:.version` 与 `marketplace.json:.metadata.version` / `.plugins[0].version` 同步为 `"2.7.2"`，未来任意一处单独漂移会立即 FAIL，避免再发生 v2.7.1 cache miss 类故障。
- **Item (b)** runtime 双路标签 + reviewer-output 文件名模板（+9 = 3 SKILL × 3 needle）：在 `.agents/skills/{review-loop,plan,execute}/SKILL.md` 三处 `Parallel Reviewer Fan-Out (N>1)` 子节绑定 `runtime: "codex"` 与 `runtime: "claude_code"` 文案，以及两行 concat needle `per-job stdout is written to\n\`.review-loop/tmp/{session_id}-reviewer-output.{job_id}.txt\`.` 唯一锚定 runtime-split 出现位置（同模板在 cleanup 段重复出现，单行 contains 无法区分）。
- **Item (c)** Edit C precedence 规则（+12 = 3 SKILL × 4 needle）：trigger（多行 `\`error\` field is non-null, or \`timed_out\` is true, or\n  \`returncode\` is non-zero` — 文本在 `or` 后换行 + 2 空格缩进，单行 needle count=0）/ classification（`classify as a **command-execution failure**`）/ precedence-tail（`diagnostic fields take precedence over stream-json parse outcome.`）/ skip-parse（`Do not attempt to parse \`stdout\` for that entry`）。
- **Item (d)** Edit D ENOENT 清理纪律 + policy 子句（+9 = 3 SKILL × 3 needle）：scheduler-half（`Per-job prompt files are scheduler-owned and may already be unlinked`）/ orchestrator failure-type（`non-ENOENT failure to delete them`）/ 多行 policy clauses（`should be logged as a warning in \`## Review History\` but must not block\nthe round verdict.` — 单行 `must not block the round verdict` 因换行 count=0，多行是唯一稳定形式）。
- **Item (e)** `parsed_verdict` / `parsed_issues` "best-effort metadata only" caveat（+4 = 1 source + 3 mirrors）：源端 `scripts/review_verification.py:12-17` 模块 docstring 锚 `Stream-json parsing here is intentionally a *best-effort* duplicate of`；3 个 SKILL 镜像锚 `parsed_verdict\` / \`parsed_issues\` are best-effort metadata only`。
- **Item (f)** `_load_jobs` 10 字段 schema list（+10）：在 `scripts/review_verification.py` 中分别守护 `entry["session_id"]` / `entry["job_id"]` / `entry.get("runtime", "codex")` / `entry.get("prompt_text", "")` / `entry.get("reviewer_model", "")` / `entry.get("timeout_secs", 300.0)` / `entry.get("conflict_keys")` / `entry.get("capacity_keys")` / `entry.get("extra_argv")` / `entry.get("worktree")` 字面量；任意字段重命名 / 默认值漂移会立即 FAIL。
- **Item (g)** protocol forward-pointer anchor（+1）：`docs/protocol/planning.md` § Reviewer dispatch forward-pointer 句 `Parallel Reviewer Fan-Out (N>1)\` subsection in each` 与三个 SKILL 已守护字符串绑定，protocol → SKILL 链接保活。
- **Item (h)** smoke `review-loop.regression.smoke.claude` 文档化（plan Option B + 保留 Option A 的扩展）：plan 原 Option A 假设 root cause 是 renderer heading drift；执行阶段经实测 fixture artifact 与 `meta.json` 后定位到真实 root cause 是 `claude -p` 在 `setup.timeout_seconds: 600` 预算内无法 converge 完整 review-loop end-to-end，runner 退到 synthetic v2.6.0 fixture 作 best-effort fallback，导致 3 项 mode-any/mode-all assertion 同时失败（不止 plan 假设的 heading drift 一项）。**Option B**：新增 `tests/skills/smoke/README.md`，明确将本 case 列入 known-flaky `best_effort` 名单，并解释 root cause + 后续路径（提高 timeout 或迁移到非真 LLM smoke harness）；本变更满足 plan acceptance criterion `either fix or document`。**Option A 保留为无害扩展**：在 `tests/skills/contracts/assertion-mapping.json` 新增 smoke assertion `execution_round_recorded_parenthetical_review_only_form` 锚 `### Round 1 (Execution / review-only)` 并加入 `execution_round_recorded` mode-any group 第 5 个成员；当未来 timeout 预算允许真 run 产出该 heading 时，该 member 会让此条 mode-any 自动 PASS。`reviewer_round_1_recorded` group 经核对未受同种 heading drift 影响，保持原状。
- **Lint baseline**：297 PASS / 0 FAIL → **345 PASS / 0 FAIL**（+48）。`tests/review_verification_test.py` 57 case 全 PASS 不变。
- **JSON 完整性**：`plugin.json` / `marketplace.json` / `tests/skills/contracts/review-loop.json` / `tests/skills/contracts/assertion-mapping.json` 全部 `json.load` 通过。
- BACKLOG P3 v2.7.1 polish-tier follow-ups bundle (8 items) 关闭。

### v2.7.1

- 将 v2.7.0 引入的 conflict-aware parallel CR scheduler (`scripts/review_verification.py`) 接入 Codex Stage 1 三个 reviewer-dispatch site：`.agents/skills/{review-loop,plan,execute}/SKILL.md` 各新增一节 `Parallel Reviewer Fan-Out (N>1)`，明确 N=1 走原 `claude -p` 单次 shell-out 路径不变（argv/stdin/模型解析/临时文件生命周期 byte-identical），N>1 走 `python3 scripts/review_verification.py --jobs <path> --output <path>` 单次外部 fan-out。
- 文档化 jobs.json schema（`session_id`/`job_id`/`runtime`/`prompt_text`/`reviewer_model`/`timeout_secs`/`conflict_keys`/`capacity_keys`/`extra_argv`/`worktree`，由 `scripts/review_verification.py` `_load_jobs` 决定）；明确 `prompt_text` 由 orchestrator 内联到 jobs.json，scheduler 自己渲染 `.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt` 并以 file-FD handoff 喂 stdin（避免 large-prompt deadlock）。
- 明确 orchestrator 是 verdict 提取与 schema 校验的唯一权威：每个 `<results.json>` 条目的 `stdout` 字段按 `docs/protocol/reviewer-output.md` shared schema 解析；scheduler 自己的 `parsed_verdict` / `parsed_issues` 仅作 metadata，per `scripts/review_verification.py:12-17`。
- `docs/protocol/planning.md` §Reviewer dispatch 的 forward pointer 由 "wiring lands in a follow-up" 改为指向上述三个具体 anchor，protocol→skill 链接保留，debt marker 解除。
- 新增 9 条 lint 断言（`codex_parallel_reviewer_fanout_anchor_*` / `codex_parallel_reviewer_scheduler_invocation_*` / `codex_parallel_reviewer_perjob_prompt_path_*`，3 needle × 3 file，inline `kind: contains`）静态守护新 prose；lint baseline 由 288 PASS / 0 FAIL 扩展到 297 PASS / 0 FAIL（无 FAIL）。
- AC narrowing 复述：本轮 wiring 仅作用于 Codex Stage 1。Claude/plugin-side `skills/{review-loop,plan,execute}/SKILL.md` 的 reviewer dispatch 是 in-process Agent-tool 调用，不走 subprocess，无法外部包装；不在本次改动范围。
- BACKLOG P2[1] follow-up "wire scheduler into Codex Stage 1 reviewer dispatch (3 sites)" 关闭。

## 2026-05-08

### v2.7.0

- 引入 `scripts/review_verification.py` — stdlib-only **conflict-aware parallel CR scheduler**（716 行），将单次 reviewer dispatch 扩展为 N 路并行 fan-out 能力。两套正交机制：(1) **二元锁** `conflict_keys`（仅 codebase invariants — `prompt_file:{session_id}:{job_id}` 与可选 `worktree:{path}`，**不含 `cli_rate:*`**）确保两个共享真实资源的 job 互斥；(2) **容量计数器** `capacity_keys`（`cli_rate:claude` / `cli_rate:codex`）opt-in 节流。默认 `Scheduler(max_parallel=4, capacity_limits=None)` 下 5 个同 runtime 的 Codex Stage 1 job 并发跑满 worker pool（peak concurrency = 4），不会被 rate-limit 串行化（解决 R1 默认 `cli_rate:*` 误锁问题）。
- `_run_one` 走 **file-FD handoff**（Approach A）：先把 prompt 写入 `.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt`，再以 `open(prompt_path, "rb")` 打开传给 `Popen(stdin=prompt_fp)` — 而非 `stdin=PIPE` + parent-side write loop（避免 large-prompt deadlock）。`os.killpg(os.getpgid(proc.pid), …)` POSIX session-isolation；SIGKILL 后 `proc.wait(timeout=5.0)` 防 D-state 挂起；`finally:` 始终 close FD + `os.unlink` 临时文件。
- **快照一次（snapshot-once）finalization** 保证 late-drain immutability：reader thread 写入 worker-frame `collections.deque`，`JobResult.stdout_bytes` 在 finalization 时取一次快照后即与 deque 解耦。`ImmutabilityOfFinishedJobsLateDrainTest` 用 `threading.Event` gate 真实驱动 `_run_one` 端到端验证。
- **Reviewer-output schema 严格解析**（`_parse_stream_json_result` + `_validate_reviewer_output_schema`）：verdict 仅接受 `APPROVE`/`REQUEST_CHANGES`；强制 `### Strengths` 出现；Issue 列表 strip 前导 `- ` 后才匹配 `[CRITICAL]`/`[MINOR]`；未支持的 severity（`[MAJOR]`/`[HIGH]`/...）直接 `schema_violation:invalid_severity` 拒绝；六类 verdict/issues 一致性违反全部命名 discriminator（`approve_with_critical` / `request_changes_without_issues` / `request_changes_minor_only` / `issues_prose_placeholder` / `issues_empty_body` / `invalid_severity`）。
- 调度可靠性 hardening：`Scheduler.submit` 在 `ex.submit` 抛异常时回滚 `_claim`（防 binary-lock 永久泄露）；reader thread 异常以 `[reader_error: ...]` 哨兵附加到 buffer 而非 silent swallow；`prompt_fp.close()` 异常处理拓宽到 `Exception`。
- **CLI front door**：`--jobs` / `--max-parallel` (默认 2) / `--default-timeout` (默认 300) / `--output` / `--text` / `--fail-on-any` / `--capacity key=N`（可重复）/ `--tmp-dir`。Exit codes: 0 clean / 1 fail-on-any-nonzero / 2 argv error / 3 scheduler-invariant violation。
- **AC #2 narrowing**：本轮 fan-out 是 **Codex Stage 1 only** 能力（包装 3 个 `claude -p` shell-out site：`.agents/skills/{review-loop:378, plan:224, execute:342}`）。Claude-side 是 in-process Agent tool，不走 subprocess 不可外部包装；spawn follow-up `BACKLOG.md` P2 跟踪 orchestrator wiring。
- 测试：57 cases / 16 test classes（mocked subprocess via `unittest.mock.patch.object`，全部 offline）— 涵盖 timeout drain / returncode propagation / immutability double-record + late-drain / 默认 conflict_keys 不含 `cli_rate:*` / parallel fan-out peak `==4` / capacity throttle `==1` / binary-lock conflict / build-argv 模型解析 (`reviewer_model > judgment_model > claude-sonnet-4-6`) / `StdinDeliveryTest`（4 子测固化 C4：file-like、basename、内容字节、非 PIPE、cleanup） / 11 个 schema 一致性 case / `output_file_missing` + `output_file_unreadable` claude_code 路径 / `worker_exception` 传播 + 锁释放。Lint baseline **288 PASS / 0 FAIL** 保持；ruff 清；mypy 清；bandit 仅 LOW 已 noqa；smoke `exit 0`（2 pre-existing FAIL 与本次代码无关）。
- 文档：`docs/protocol/planning.md` §Reviewer dispatch 加 forward-pointer note，`CLAUDE.md` Codex Stage 1 Notes 加 1 bullet 指向 scheduler，`BACKLOG.md` P2[1] 标 in-progress 并 spawn follow-up "wire scheduler into Codex Stage 1 reviewer dispatch (3 sites)"。
- 计划 4 轮 + 执行 3 轮 codex/gpt-5.5 reviewer 迭代（session `f1c0fa2f-2371-46d8-92cd-d02302b174b2`）：plan R1 → R4 APPROVE（fix `cli_rate:*` default + wrapper anchors + late-drain test gap + R3 risk reframing + C3 contract citation 修正 + AC narrowing + C4 stdin gap + session_id/tmp_dir 显式化）；execute R1 → R3 APPROVE（fix LateDrain test theater + ParallelFanOut bypass + parser schema laxity + unknown severity 拒绝 + issues_empty_body direct case）；polish 综合修复 4 critical 测试 + 4 高优 hardening + 4 simplifier 抽取。Final-boss BACKLOG P2 [1] 完成。
- Plugin v2.6.33 → **v2.7.0** milestone bump（不是 patch — 引入新公共 `scripts/` library 接口，是显著能力扩展）。

### v2.6.33

- Delivery Summary language pinned to 中文 (Simplified Chinese) at the protocol SSOT (`docs/protocol/execution.md` §Step 4 — Delivery #2 / #3). Section headings, prose, and prose-style field values render in 中文; ASCII tokens (file paths, identifiers, SHAs, CLI flags, model names, status enums such as `APPROVE` / `CRITICAL`) stay in their original form. Rule is runtime-agnostic — both Claude Code and Codex Stage 1 inherit via existing "Per `docs/protocol/execution.md` §Step 4 — Delivery" references in `skills/review-loop/SKILL.md`, `skills/execute/SKILL.md`, and `.agents/skills/execute/SKILL.md` (no per-runtime mirror to avoid double-maintenance). The `docs_file` appended copy preserves the same 中文 rendering as the terminal summary. Lint baseline 288 case-level PASS / 0 FAIL preserved.

### v2.6.32

- B3 (`tool_use_min_count`) tightened from `min: 0` (vacuous-pass) to `min: 1` (real-catch) on 7 truncating smoke fixtures via per-fixture `setup.timeout_seconds` bumps. Branch B (linear scale-up) selected after live single-fixture re-measurement at v2.6.31: `execute.session-resume.smoke.claude` failed at 180s (returncode -15 / status skip) and passed at 240s (`status: pass`, 1 Agent event with `subagent_type: general-purpose`). Locked tier values: NEAR_DISPATCH ×3 → 240s (session-resume, stop-after-before-security, stop-after-polish); IN_DOC_RECON ×3 → 360s (from-plan, review-only, stop-after-before-polish); full-pipeline → 600s (review-loop.regression). Each fixture's B3 override flipped `min: 0 → 1` and `_comment` refreshed to `min: 1 enforced at <Ns> per ADR-4` atomically with the timeout bump in the same Edit. `plan.fresh.smoke.claude` is unchanged (control fixture; already passes B3 with implicit shared `min: 1`). `tests/skills/contracts/assertion-mapping.json` shared default unchanged (AC-5). Recorded as ADR-4 (extends ADR-2; ADR-2 not mutated, append-only). The pass/fail criterion under which Branch B was locked is `meta.status == "pass"` AND ≥1 Agent event with `subagent_type: general-purpose`, NOT literal `returncode == 0`: under `execution_policy: best_effort` the runner is by-design permitted to return SIGTERM at the timeout cap (`returncode == -15`) while still stamping `meta.status="pass"` if assertions hold on the captured partial event stream (the `timed_out_with_passing_state` gate at `scripts/run-skill-smoke` line 987). 240s/360s/600s values are extrapolated from the spike's event-rate model and validated empirically only at the NEAR_DISPATCH tier (single-probe scoping per HANDOFF Codex-hang practice); IN_DOC_RECON 360s and full-pipeline 600s remain best-guess until a future re-measurement falsifies them. Worst-case CI wall time approximately 41 minutes per Branch B.
- Closes BACKLOG P3-2 (B3 truncation tightening) — see ADR-4.

### v2.6.31

- `scripts/replay_sessions.py` error-paths hardening — sub-scope (a), genuine plan-locked contract change. `scan_file` now narrowly catches `OSError` (covers `PermissionError`, `FileNotFoundError` race-vs-glob, `IsADirectoryError`, transient FS) at the `read_text` + `stat` call site, writes one stderr line `replay_sessions: unreadable file: <path>: <reason>`, and returns `None`. `build_report` skips `None` records and surfaces the count under a new fourth summary key `summary["unreadable_files"]`. `main` adds a distinct exit code `3` (after `--exit-zero` short-circuit, before anomaly exit-1) so I/O failures aren't conflated with anomaly-detected exit `1`. `render_text` summary footer appends `unreadable=N`. Locked by new `UnreadableFileTest` class with 6 in-process methods using `unittest.mock.patch.object(Path, "read_text", autospec=True, side_effect=...)` plus a `_selective_read_text` helper: exit-3 alone, exit-3 outranks exit-1 (Q2 pin), `--exit-zero` suppresses exit-3 (Q3 pin), unreadable files skipped from `report["files"]` (Q4 pin), stderr line format + glob-sort order (AC-3 pin), `FileNotFoundError` race falls through `OSError` catch (AC-1 enumeration pin). Three pre-existing summary-shape assertions updated explicitly (key list, empty-directory dict + length). Test count 75 → 81. Lint baseline 288 case-level PASS / 0 FAIL preserved. No downstream consumer impact: only `tests/replay_sessions_test.py` references `summary` keys / exit codes; no CI / smoke / docs / contract consumers.
- Closes BACKLOG P3 (`scripts/replay_sessions.py` error-paths sub-scope (a)) — completing the silent-failure-hunter HIGH follow-up filed during v2.6.25 polish (sub-scope (b) shipped in v2.6.30).

### v2.6.30

- 8 new tests close 2 P3 backlog items in a bundled delivery (per v2.6.28 controlled-deviation precedent — pure-test, mutually independent, no shared mutation surface): `RunParserHelperContractTest` (1 method, P3 [3b] — `tests/replay_sessions_test.py::run_parser` helper hardening: replaces silent `parsed = None` JSONDecodeError fallback with `AssertionError` carrying subprocess stdout/stderr/returncode, so a parser regression surfaces a clean failure at the assertion site instead of a confusing downstream `TypeError`); `SecondTierCoverageTest` extended with 4 methods (P3 [4] gaps 1, 2 negative + positive, 5 — `bt` quoted regex branch isolated; secondary regex `BARE_REVIEW_LOOP_RE` left-boundary current behavior pinned (negative + positive); `render_text` `>50`-char path-truncation branch); `KindContainsTest` extended with 3 methods (P3 [4] gaps 3, 4 — case-sensitivity invariant pin (uppercase needle matches uppercase, fails on lowercase); empty-needle rejected at contract-load time by `require_fields`). `test_glob_is_single_level_not_recursive` augmented with positive existence assertion (Gap 6). No script-under-test, contract-loader, or lint contract JSON edits. Test count 67 → 75. Lint baseline 288 case-level PASS / 0 FAIL preserved.
- Closes BACKLOG P3 [3b] (`run_parser` helper hardening) and P3 [4] (six third-tier coverage gaps).

### v2.6.28

- 10 new tests close 2 P3 backlog items: `KindContainsTest` (3 methods, isolates `kind: contains` lint mechanic from integration smoke) and `SecondTierCoverageTest` (7 methods covering 6 gaps — gap 1 split into sq + dq quote branches; plus secondary-regex right-boundary, errors=replace UTF-8 decode, *.md non-recursive glob, anomaly_values set-dedup, --text rendering pinning). No script-under-test changes. Test count 57 → 67.
- Closes BACKLOG P3 (KindContainsTest unit class + second-tier replay_sessions coverage).

## 2026-05-07

### v2.6.26

- 15 new tests in `tests/replay_sessions_test.py` close 7 MEDIUM coverage gaps from pr-test-analyzer's v2.6.25 quality-polish pass — `--root` non-directory / non-existent exit-code-2 path, multi-file aggregation, empty-directory contract, same-value-multi-line counts, `anomaly_sites` line-number fidelity, JSON `sort_keys` / per-file `mtime` ISO 8601 invariants, plus in-process `ScanLineUnitTest` and `BuildReportUnitTest` covering `scan_line` / `build_report` directly. No parser change. Test count 14 → 29.
- Closes BACKLOG P3 (replay_sessions test plumbing-edge gaps).

## 2026-05-07

- review-loop v2.6.25: new `scripts/replay_sessions.py` — stdlib-only post-hoc audit channel that walks `.review-loop/sessions/*.md`, enumerates every `subagent_type` value per file, and flags `review-loop:*` occurrences as anomalies. Uses an anchored primary regex (`\bsubagent_type:`) plus a closed-set bare-form secondary regex over the 12 known agent names, with span-overlap dedup so a single occurrence isn't double-counted. Emits JSON by default (`--text` for a human table); exit 1 on anomaly, 0 otherwise (`--exit-zero` overrides). Complements the existing static lint contract for `SKILL.md` / protocol-doc files. Backed by 14 unittest cases (4 acceptance + 7 corpus-grounded with verbatim file:line citations + 1 dedup + 2 CLI).
- Closes BACKLOG P2 (session-replay parser).

## 2026-05-07

- review-loop v2.6.24: per-site `contains` companion assertions for 6 codex `pattern_requires_adjacent` stop-and-surface anchors in `tests/skills/contracts/review-loop.json`. Mirrors the existing `codex_execute_git_diff_failure_present`/`_lock_release` template across plan/SKILL.md and execute/SKILL.md so any future removal of an anchor needle FAILs lint instead of silently passing.
- Lint baseline 272 → 278 case-level PASS / 0 FAIL; unit tests 28/28 OK.
- Closes BACKLOG P3 (per-site contains companion for 6 stop-and-surface anchors). Claude side remains out-of-scope per the v2.6.23 mapping (no Claude analogue; SKILLs delegate to `docs/protocol/planning.md §Reviewer dispatch`).

## 2026-04-23

- review-loop: Codex Stage 1 now follows the same downstream `exec -> polish -> docs -> security -> delivery` lifecycle as Claude Code instead of silently stopping at `exec`.
- Protocol docs, repo skills, plugin mirrors, README, and guide surfaces were aligned on the widened delivery gate, clean stop points, and the real `quality_focus` / `skip_quality_polish` semantics.
- Contract and smoke coverage were expanded for Codex reviewer routes, review-only routing, `skip_quality_polish`, and the missing `before-polish` / `before-security` stop seams.
- Delivery hygiene tightened: plugin metadata bumped to `2.6.18`, stale developer docs were refreshed, and `.gitignore` gained the security-preflight pattern coverage defined in `docs/protocol/execution.md`.
