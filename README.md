# Bilibili Innocent Builds

> [!CAUTION]
> **极度不稳定的实验构建——比 Canary 更不稳定。**
> 本仓库的 APK 仅供抢先尝鲜，或验证某个特定 Hotfix。可能存在崩溃、无法启动、Hook 失效、功能不完整及各种不适配，不建议作为日常版本。CI 通过不代表实机回归通过，也不代表某个修复已经确认有效。

独立构建 [LiJiaHua1024/Bilibili_Innocent_Lab](https://github.com/LiJiaHua1024/Bilibili_Innocent_Lab) 的任意已推送分支，使用自己的固定证书签名。Fork 的 `main` 无需加入本仓库的提交，可以继续快进同步上游。

实验包的发布规范与反馈要求见 [EXPERIMENTAL_RELEASES.md](EXPERIMENTAL_RELEASES.md)。功能成熟后争取合并上游并由上游正式发布；本仓库只承担实验分发和修复验证。

## 实验预发布

同一批分支 APK 集中到 [同一个 Pre-release](https://github.com/LiJiaHua1024/Bilibili_Innocent_Builds/releases)，共用一个日期标签。标题使用 `Nightly Build YYYYMMDD`，例如 `Nightly Build 20261025`，不标为 Latest。

Release 只上传最终 Release APK，不上传 ZIP、Build Info、哈希文件或测试报告。极不稳定声明放在发布说明顶部，APK 文件名只保留 `BIL-<branch-slug>-<sha12>.apk`。各分支独立，不是功能合集，请按文件名选择需要测试的分支。

## 一键构建并发布

打开 Actions 中的 **Build and publish Nightly**，选择本构建仓库的 `main`，在 **source_branches** 中填写要更新的 Fork 分支。可以填一个分支、逗号分隔的多个分支，或 `all`（不含 Fork 的 `main`）。

这次点击表示构建成功后自动创建实验标签并公开发布预发布。GitHub 接收任务后即可关闭本机，后续构建、签名和发布均在 GitHub 托管 runner 上完成。

```powershell
gh workflow run nightly-release.yml --repo LiJiaHua1024/Bilibili_Innocent_Builds --ref main -f source_branches=feat/npatch-compat
```

流水线会先固定源码提交，只重建相对上一份已发布 Nightly 有变化的所选分支，其余 APK 沿用上一版。例如只更新 NPatch 时，新 Release 仍包含六个包，其中一个是新构建，另外五个保持原字节。选择的分支全都没有源码变化时，成功结束并跳过构建和发布。

发布文案沿用维护者手改的 [Nightly Build 20261025](https://github.com/LiJiaHua1024/Bilibili_Innocent_Builds/releases/tag/nightly-20261025)：保留简短的不稳定说明、分支表格和结尾。每次列出更新或首次加入的分支，附源码对比链接；表格标明更新、首次加入或沿用，保留 APK、源码提交和 CI 链接。详细提交记录通过链接查看，不在正文重复堆放；分叉、回退或无法比较时保留简短说明。不需要额外 LLM API 密钥。

标题和标签按发布时的北京时间生成：`Nightly Build 20261026` / `nightly-20261026`；同一天再次发布依次使用 `.2`、`.3`，不覆盖旧快照。任何选中分支构建失败、签名/哈希不符，或构建期间上一版 Nightly 被其他操作改动，都不公开新 Release。

失败后可重试本次工作流的失败任务。发布器可以复用同一次运行里先前已经成功的签名产物；若发布响应丢失，会读取并恢复原来的草稿或已发布结果，避免再发一份。残缺或内容不同的草稿附件会停止并要求检查，不覆盖它们。

沿用包需要从上一版 Release 下载，这些自动化请求可能计入旧附件的下载次数，不代表有相同数量的外部用户下载。发布说明中的隐藏 JSON 注释只保存分支与包的来源状态，不新增附件；请保留它。第一份手工发布的 Nightly 无需补这个注释，脚本会从现有分支表和 APK 名称识别。

## 构建一个分支

1. 打开 [Build signed branch APK](https://github.com/LiJiaHua1024/Bilibili_Innocent_Builds/actions/workflows/branch-build.yml)。
2. 点击 **Run workflow**。GitHub 自带的 **Use workflow from** 选择本构建仓库的 `main`。
3. 在 **source_branch** 填写 Fork 的分支完整名称，例如 `main`、`feat/npatch-compat`、`experiment/ai-insight`、`experiment/audio-enhance`、`experiment/lan-continuity-cast`、`feature/incognito-no-login` 或 `opt/power-saving`。
4. 等待 `build` 与 `sign` 两个任务成功，从本次运行的 Artifacts 下载以 `UNSTABLE-EXPERIMENTAL-` 开头的最终安装包。早期工作流产物仍以 `branch-` 开头。

所选分支不需要合并，也不需要包含本工作流。本流程只拉取 Fork 已推送的源码，不包含本地未提交或未推送的修改。这个 **Build signed branch APK** 入口仅构建，不创建 Release；需要发布时使用上面的独立入口。两个入口都不发送 Telegram 消息，也没有配置每日定时触发。

也可以在登录 GitHub CLI 后执行：

```powershell
gh workflow run branch-build.yml --repo LiJiaHua1024/Bilibili_Innocent_Builds --ref main -f source_branch=feat/npatch-compat
gh run list --repo LiJiaHua1024/Bilibili_Innocent_Builds --workflow branch-build.yml
```

## 集成模式内测构建

面向内测群的 **Build signed embedded APK** 单独构建 `:app:assembleEmbedded`：与 Release 同形态、同固定签名，包名为 `com.Bilibili_Innocent_Lab.xposedmodule.embedded`，可与正式模块并存。构建任务只使用一次性密钥，固定证书只在隔离签名任务中用于重签，源码分支代码不会在持有长期私钥的 runner 上执行。

```powershell
gh workflow run embedded-build.yml --repo LiJiaHua1024/Bilibili_Innocent_Builds --ref main -f source_branch=feat/npatch-compat
gh run list --repo LiJiaHua1024/Bilibili_Innocent_Builds --workflow embedded-build.yml
```

产物 artifact 以 `EMBEDDED-` 开头，保留 30 天，R8 符号另存 14 天。该入口只构建并上传 artifact，不创建 Release，也不进入 Nightly 汇总。源码侧的构建变体、包名与签名约定见 Fork 分支内的 `docs/embedded-test-build.md`。

## Actions 产物和验证

最终产物包含 `app-release.apk`、显著的不稳定性说明、APK 文件 SHA-256、签名校验结果、APK 包信息和 `build-info.txt`。记录中分别注明源码仓库、分支、实际提交 SHA，以及构建工作流仓库和提交 SHA。最终产物保留 30 天，JVM 测试、完整 Lint 报告和 R8 符号保留 14 天。

这些辅助文件仅保留在 Actions Artifacts 中用于核验，不作为 Release 附件上传。

以 `intermediate-` 开头的产物用于两个任务间传递，使用一次性签名，保留 1 天；不要把它当成最终版本安装。

`build` 使用 JDK 17、Android SDK 37、Build Tools 37.0.0、NDK 29.0.14206865、CMake 3.31.5，运行 Release/R8 构建、JVM 单测和完整 Lint。现有分支的签名配置通过 `INNOCENT_LAB_SIGNING_*` 环境变量接收。工具链不同的历史分支可能需要适配，不能保证任何历史代码都通过检查。

各分支保留自己的版本名和版本码。同包名、同签名的分支包会更新同一个应用，不能并行安装；版本码降低时 Android 可能拒绝更新。本工作流不自动修改版本码，也不清除手机应用数据。首次从上游签名或本地 Debug 签名切换到自己的发布签名时，不能直接当成同签名更新。

## 固定签名

长期私钥只保存在本构建仓库的 Environment Secrets 中，不放进源码或 Artifacts。

| Environment | 用途 |
| --- | --- |
| `alpha-release` | 分支 APK 的隔离签名任务 |
| `stable-release` | 保存同一签名，供未来正式发布流程使用；当前没有 Stable 发布工作流 |

两个环境都只允许构建仓库的 `main` 访问。这个限制针对工作流所在分支，源码仍可选择 Fork 的任意分支。

环境内需要 `ANDROID_SIGNING_KEY_BASE64`、`ANDROID_SIGNING_STORE_PASSWORD`、`ANDROID_SIGNING_KEY_ALIAS`、`ANDROID_SIGNING_KEY_PASSWORD` 和 `ANDROID_SIGNING_CERT_SHA256`。

本机初始化或重新导入由 `scripts/setup-signing.ps1` 完成。使用 PowerShell 7 与 JDK 17，在 GitHub CLI 已登录后运行：

```powershell
$env:JAVA_HOME = 'C:/Program Files/Java/jdk-17'
$env:Path = "$env:JAVA_HOME/bin;$env:Path"
./scripts/setup-signing.ps1 -RequireExisting
```

默认复用用户目录下 `Documents/AndroidSigning/Bilibili_Innocent_Lab/innocent-lab-release.p12`，提示输入已有密码并写入新仓库的两个环境。可用 `-KeyStorePath` 指定离线备份位置；`-RequireExisting` 在密钥不存在时直接报错，避免迁移时意外换证书。仅在首次创建全新身份时才省略此参数。密钥文件和密码应分别备份；以后每次发布继续复用同一密钥。

构建 runner 只使用临时密钥，不关联签名环境。构建通过后，签名任务在全新 runner 上通过精确 artifact ID 下载中间 APK，并要求摘要校验通过。它不检出或执行 Fork 的代码，使用官方 `apksigner` 替换临时签名，校验证书 SHA-256、单一签名者及非 Debug 属性，再上传最终 APK。

自动发布 runner 仅运行本仓库固定提交的发布工具，不执行 Fork 代码，也不读取长期签名私钥。只有这个任务持有 BIB 的 `contents: write`；编译和规划任务保持只读。发布前还会检查所有 APK 的固定证书、包名和非 Debug 属性。更换证书时，需要同步审阅 `scripts/nightly_release.py` 中的公开证书指纹。

## 维护

源代码更新只发生在 Fork；构建配置更新只发生在本仓库。未来新增分支直接填写其名称即可，无需改工作流。签名仍可在 Fork 的现有环境中保留，两边使用同一证书；GitHub Secrets 不会跨仓库自动共享。

发布工具使用 Python 3.12+ 与 uv，无第三方 Python 依赖。开发时运行 `uv sync --locked`，再用 `uv run --locked python -m unittest discover -s tests -v` 检查增量规划、包校验、发布说明、失败恢复和重试幂等性。

串行发布使用 GitHub 当前支持的 [`concurrency.queue: max`](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)，保留排队请求。actionlint 1.7.12 尚不认识这个字段；本地检查仅针对这一条已核实的新语法使用精确兼容过滤，其余规则不跳过。首次上线后仍需完成云端构建、环境 Secrets 和发布的联动验证。
