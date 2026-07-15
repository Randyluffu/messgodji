@echo off
chcp 65001 >nul
setlocal
echo ===================================================
echo   Godji Messenger - sborka exe (admin + klient)
echo   Zapuskat odin raz na lyubom Windows PC s Python.
echo   Gotovye exe potom rabotayut na lyubom PC bez Python.
echo ===================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python ne nayden v PATH.
    echo     Skachay i postav: https://www.python.org/downloads/
    echo     Pri ustanovke OBYAZATELNO otmet galku "Add python.exe to PATH".
    echo     Posle ustanovki zapusti etot fayl zanovo.
    pause
    exit /b 1
)

echo === Ustanavlivayu zavisimosti (mozhet zanyat paru minut) ===
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [!] Ne udalos obnovit pip, no probuyu prodolzhit...
)

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo.
    echo [!] Ne udalos ustanovit zavisimosti - smotri tekst oshibki VYSHE.
    echo     Chastye prichiny: net interneta, staraya versiya Python,
    echo     antivirus zablokiroval pip.
    pause
    exit /b 1
)

echo.
echo === Generiruyu ikonki ===
python generate_icons.py
if errorlevel 1 (
    echo [!] Ne udalos sgenerirovat ikonki, no prodolzhayu bez nih...
)

echo.
echo === Sobirayu adminskiy exe ===
if exist admin_icon.ico (
    python -m PyInstaller --noconsole --onefile --icon=admin_icon.ico --name GodjiMessengerAdmin messenger_admin_app.py
) else (
    python -m PyInstaller --noconsole --onefile --name GodjiMessengerAdmin messenger_admin_app.py
)
if errorlevel 1 (
    echo [!] Sborka adminskogo exe upala - smotri oshibku vyshe.
    pause
    exit /b 1
)

echo.
echo === Sobirayu klientskiy exe ===
if exist client_icon.ico (
    python -m PyInstaller --noconsole --onefile --icon=client_icon.ico --name GodjiMessengerClient messenger_client_app.py
) else (
    python -m PyInstaller --noconsole --onefile --name GodjiMessengerClient messenger_client_app.py
)
if errorlevel 1 (
    echo [!] Sborka klientskogo exe upala - smotri oshibku vyshe.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo   Gotovo! Fayly v papke dist\
echo     dist\GodjiMessengerAdmin.exe   -^> na adminskiy PC
echo     dist\GodjiMessengerClient.exe  -^> na igrovye PC
echo   Python na igrovyh PC NE nuzhen - prosto kopiruesh exe.
echo ===================================================
pause
