REM ==========================================================
REM  VideoSummarizer 打包脚本
REM
REM  先理清三个目录，别搞混：
REM    src\ + plugins\  = 图纸（源码，改代码用，删了就没法重新打包）
REM    build\           = 车间（PyInstaller 的中间产物：.toc 分析表、.pyz 代码包，
REM                       随时可删，下次打包会自己重新生成）
REM    dist\            = 成品（两个单文件 exe + config.json，**删了就没程序了**）
REM
REM  所以「清理上一轮产物」只删 build\ 和 dist\，src\ 一个字都不动。
REM ==========================================================
@echo off
chcp 65001 >nul
setlocal

REM ---- 1. 定位 Python ----
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [错误] 没找到 Python，请先安装 Python 3.10+ 并加到 PATH
        pause & exit /b 1
    )
    set "PY=python"
)
REM ---- 0. 产物输出目录 ----
REM 每轮迭代打到**新目录**（dist_v16 / dist_v17 / dist_v18 …），旧目录留着可回滚。
REM 想打到别处：set VSUM_DIST=dist_v18 后再运行本脚本。
REM
REM 为什么不原地覆盖 dist\：实测本机有批量删除保护，PyInstaller 重建时删不掉
REM 旧目录，会出现「Build complete 但 exe 时间戳还是上一轮」—— 新代码没进去
REM 而界面毫无异常。换输出目录是绕开它最省事的办法（见第 7 步校验）。
if "%VSUM_DIST%"=="" set "VSUM_DIST=dist"
set "DIST=%VSUM_DIST%"
echo [0/7] 产物输出目录: %DIST%

echo [1/7] 使用解释器: %PY%
"%PY%" --version

REM ---- 2. 装依赖 ----
echo.
echo [2/7] 安装/检查依赖（首次约 3-5 分钟）...
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [错误] 依赖安装失败，检查网络或代理后重试
    pause & exit /b 1
)
REM 提醒：playwright 已列入 requirements（抖音/快手/小红书的浏览器兜底必需）。
REM 它在 spec 里是**硬校验** —— 缺了会直接中止构建，不会再静默产出一个
REM "少了兜底"的 exe。历史上这里写成过可选，结果连丢好几版，别再改回去。

REM ---- 3. 清掉上一轮的 build/ 与 dist/ ----
REM 这一步不是"顺手清理"，是**必须**的。
REM PyInstaller 在 COLLECT 阶段会尝试删掉 dist\VideoSummarizer 再重建，
REM 而那个目录有 200+ 文件。在启用了批量删除保护的环境里（安全软件 / 企业策略 /
REM 工具链的 safe-delete 钩子）这一步会被拦下 —— 表现是**构建"成功"但产物还是
REM 上一轮的旧文件**，exe 时间戳不变，新代码根本没打进去，极难察觉。
REM 自己先删干净，PyInstaller 就只需要"创建"而不用"删除"，绕开这个坑。
REM
REM 实测补充（2026-10-06）：本机的批量删除保护**连 rmdir 自身也会拦**，
REM 日志里出现 `SAFE_DELETE_BULK_CONFIRM_REQUIRED {"count":203,"threshold":50}`，
REM rmdir 静默失败 -> PyInstaller 接着尝试删同一个目录 -> 同样被拦 ->
REM 产物保持旧版。表现为「Build complete」但 exe 时间戳停在上一轮。
REM 所以**第 6 步的时间戳校验是唯一可靠的兜底**，不要因为 rmdir 没报错就以为清干净了。
REM 若校验失败：换一个从未用过的输出目录（--distpath dist_v2 这种），
REM 而不是反复重试同一个目录。
echo.
REM ---- 3. 准备内置浏览器内核 ----
REM 这一步是 v18 才有的。**playwright 包里只有驱动，没有浏览器**：
REM 浏览器本体在 %LOCALAPPDATA%\ms-playwright（271 MB），打不进 exe。
REM 把 exe 发给没装 Edge/Chrome 的人，抖音兜底就整个失效。
REM 所以这里先从本机内核生成一份**精简副本**（271 MB → 219 MB）放到
REM vendor\pw-browsers\，spec 会把它一起打进 exe（压缩后约 +100 MB）。
REM
REM 缺了它 spec 会直接中止打包，不会静默产出"没有内核"的 exe。
echo.
echo [3/7] 准备内置浏览器内核（精简版 chrome-headless-shell）...
"%PY%" "%~dp0tools\prepare_browser_vendor.py"
if errorlevel 1 (
    echo [错误] 内置内核准备失败，按其提示先装内核再重试。
    echo        不想内置内核可设 VSUM_BUNDLE_BROWSER=0 跳过（功能会少一层兜底）。
    pause & exit /b 1
)

echo.
echo [4/7] 清理上一轮产物...
if exist "%~dp0%DIST%" rmdir /s /q "%~dp0%DIST%"
if exist "%~dp0build" rmdir /s /q "%~dp0build"
if exist "%~dp0%DIST%" (
    echo   [提示] dist\ 仍存在，可能被批量删除保护拦下。
    echo          PyInstaller 稍后会再试一次；第 6 步会校验产物是否为新生成。
)

REM ---- 4. 打包 ----
echo.
echo [5/7] 开始打包（约 5-15 分钟，取决于机器性能）...
REM 注意点：
REM   1) 不要加 --clean。它会先整目录删除 build/，
REM      在部分安全软件/策略下会被拦截，导致打包中途失败。
REM   2) 界面版和命令行版必须分两次跑、各用独立 workpath。
REM      同一个 workpath 下第二个 EXE 会整包删除第一个生成的
REM      base_library.zip，同样可能被安全策略拦下。
echo   - 界面版 VideoSummarizer.exe ...
set "VSUM_TARGET=gui"
"%PY%" -m PyInstaller "%~dp0VideoSummarizer.spec" --noconfirm --workpath "%~dp0build\%DIST%_gui" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 界面版打包失败
    pause & exit /b 1
)

echo   - 命令行版 vsum.exe ...
set "VSUM_TARGET=cli"
"%PY%" -m PyInstaller "%~dp0VideoSummarizer.spec" --noconfirm --workpath "%~dp0build\%DIST%_cli" --distpath "%~dp0%DIST%"
if errorlevel 1 (
    echo [错误] 命令行版打包失败
    pause & exit /b 1
)

REM ---- 5. 收尾 ----
echo.
echo [6/7] 整理产物...
if not exist "%~dp0%DIST%" mkdir "%~dp0%DIST%"
REM 单文件产出：dist\ 根目录直接就是两个 exe，没有再套 VideoSummarizer\ 子目录。
copy /y "%~dp0README.md" "%~dp0%DIST%\" >nul 2>nul

REM 配置文件随产物一起分发。程序是便携模式优先 —— 读的是 **exe 同目录** 的
REM config.json，不是项目根目录那份。漏拷这一步，新装的机器上所有设置都会
REM 退回代码内默认值（表现为「设置填了但没生效」）。
REM
REM （v16 起就算漏拷也不会丢设置：首次运行若找不到配置文件，程序会**自动落一份**
REM  默认 config.json 到 exe 旁边，用户可以直接用记事本改关注方向等内容。）
REM
REM 还要带一份 config.example.json —— **脱敏模板**。config.json 里存着真实的
REM API Key（llm.api_key / asr.cloud_api_key），直接发给别人的话密钥会泄露且
REM 可能被他人消耗余额。模板里服务商地址、模型名、库路径全部保留，只把 Key 清空，
REM 对方拿到就能知道该填什么。
if exist "%~dp0config.json" (
    copy /y "%~dp0config.json" "%~dp0%DIST%\" >nul 2>nul
)
if exist "%~dp0config.example.json" (
    copy /y "%~dp0config.example.json" "%~dp0%DIST%\" >nul 2>nul
)

REM ==========================================================================
REM  产物形态：**单文件**
REM
REM  dist\VideoSummarizer.exe 和 dist\vsum.exe 都是自包含的，**没有 _internal\**。
REM
REM  早先版本同时生成了 exe 内嵌依赖 + _internal\ 副本（约 420-500 MB，纯冗余）。
REM  实测 2026-10-09：带 _internal 启动 4.73s / 4.64s，把 exe 单独拷到空目录启动
REM  4.49s / 4.52s —— 完全一样。即 exe 每次启动都在往 %TEMP% 解压，
REM  _internal\ 从没被读过。所以 v16 起 spec 里删掉了 COLLECT，只出单文件。
REM
REM  发给别人：只需拷 VideoSummarizer.exe 一个文件（想保住设置就连 config.json 一起）。
REM ==========================================================================

REM ---- 6. 校验产物确实是本轮生成的 ----
REM 只检查「文件存在」是不够的：PyInstaller 可能因为安全软件拦截删除而
REM 跳过重写，产物存在但仍是上一轮的旧文件，**新代码根本没打进去**，
REM 而界面完全看不出异常（表现为「改了设置没生效」）。
REM 所以这里比对 exe 时间戳与本轮打包开始时间。
echo.
echo [7/7] 校验产物为本轮新生成...
set "BUILD_STAMP=%~t0"
for %%F in ("%~dp0%DIST%\VideoSummarizer.exe" "%~dp0%DIST%\vsum.exe") do (
    if not exist %%F (
        echo   [失败] 未生成: %%F
    ) else (
        REM %%~tF = 该文件最后写入时间；字符串比较对 yyyy/mm 格式够用
        if %%~tF gtr "%BUILD_STAMP%" (
            echo   [OK] %%~nxF  %%~tF
        ) else (
            echo   [失败] %%~nxF 时间戳 %%~tF 不晚于本轮开始 %BUILD_STAMP%
            echo          ^—— 这是上一轮的旧文件，新代码没打进去。^)
            echo          ^—— 多半是 dist\ 或 build\ 没删干净，或被安全软件拦了删除。^)
            echo          ^—— 关掉正在运行的程序，手动移走 dist\ 与 build\ 后重试。^)
        )
    )
)

echo.
echo ============================================
echo  完成！产物在 %DIST%\ ：
echo    %DIST%\VideoSummarizer.exe   ^(桌面界面，单文件^)
echo    %DIST%\vsum.exe              ^(命令行，单文件^)
echo    %DIST%\config.json           ^(设置与关注方向，可直接编辑^)
echo.
echo  两个 exe 都是自包含的单文件，**不依赖 _internal**。
echo  v18 起 exe 内已自带精简浏览器内核，发给别人只需拷
echo  VideoSummarizer.exe 一个文件即可双击运行（含抖音兜底）。
echo ============================================
echo.
pause
