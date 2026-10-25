# Bilibili Innocent Builds

独立构建 [LiJiaHua1024/Bilibili_Innocent_Lab](https://github.com/LiJiaHua1024/Bilibili_Innocent_Lab) 的任意已推送分支，使用自己的固定证书签名。Fork 的 `main` 无需加入本仓库的提交，可以继续快进同步上游。

## 构建一个分支

1. 打开 [Build signed branch APK](https://github.com/LiJiaHua1024/Bilibili_Innocent_Builds/actions/workflows/branch-build.yml)。
2. 点击 **Run workflow**。GitHub 自带的 **Use workflow from** 选择本构建仓库的 `main`。
3. 在 **source_branch** 填写 Fork 的分支完整名称，例如 `main`、`feat/npatch-compat`、`experiment/ai-insight`、`experiment/audio-enhance`、`experiment/lan-continuity-cast`、`feature/incognito-no-login` 或 `opt/power-saving`。
4. 等待 `build` 与 `sign` 两个任务成功，从本次运行的 Artifacts 下载以 `branch-` 开头的最终安装包。

所选分支不需要合并，也不需要包含本工作流。本流程只拉取 Fork 已推送的源码，不包含本地未提交或未推送的修改。构建手动触发，不自动创建 GitHub Release 或发送 Telegram 消息。

也可以在登录 GitHub CLI 后执行：

```powershell
gh workflow run branch-build.yml --repo LiJiaHua1024/Bilibili_Innocent_Builds --ref main -f source_branch=feat/npatch-compat
gh run list --repo LiJiaHua1024/Bilibili_Innocent_Builds --workflow branch-build.yml
```

## 产物和验证

最终产物包含 `app-release.apk`、APK 文件 SHA-256、签名校验结果、APK 包信息和 `build-info.txt`。记录中分别注明源码仓库、分支、实际提交 SHA，以及构建工作流仓库和提交 SHA。最终产物保留 30 天，JVM 测试、完整 Lint 报告和 R8 符号保留 14 天。

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

## 维护

源代码更新只发生在 Fork；构建配置更新只发生在本仓库。未来新增分支直接填写其名称即可，无需改工作流。签名仍可在 Fork 的现有环境中保留，两边使用同一证书；GitHub Secrets 不会跨仓库自动共享。
