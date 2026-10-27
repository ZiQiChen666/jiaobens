@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 CreatureCurios 后台按键器 v2

echo ============================================
echo  CreatureCurios 后台按键器 v2 - 打包 exe
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [提示] 正在安装 pyinstaller ...
    python -m pip install pyinstaller
    if errorlevel 1 goto fail
)

echo [1/2] 清理旧的 build / dist ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/2] 开始打包，请稍候（约 1-3 分钟）...
python -m PyInstaller --noconfirm --clean CreatureCuriosKeyer.spec
if errorlevel 1 goto fail

echo.
echo 打包完成： %cd%\dist\CreatureCuriosKeyer.exe
echo 提示：如果游戏以管理员权限运行，请右键 exe 选择“以管理员身份运行”。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 打包出错，请查看上方报错信息。
pause
exit /b 1
