@echo off
chcp 65001 >nul
title Forum → Discord izleyici
cd /d "%~dp0"

REM py yoksa python dene (Windows Store takılmaları için: Ayarlar → Uygulama diğer adları → python.exe kapatılabilir)
where py >nul 2>&1 && set "PY=py" || set "PY=python"

echo [1/2] Son ileti Discord'a gonderiliyor...
%PY% forum_discord_webhook.py --notify-latest
if errorlevel 1 (
  echo Calistirma hatasi. Yukaridaki mesaji oku.
  pause
  exit /b 1
)

echo.
echo [2/2] 10 dakikada bir kontrol basladi. Bu pencereyi kapatma; kapatinca izleme durur.
echo Baslangica eklemek icin bu dosyaya sag tik — Tumuyle calistir veya kisayolu buraya kopyala:
echo   %%APPDATA%%\Microsoft\Windows\Start Menu\Programs\Startup
echo.

%PY% forum_discord_webhook.py --interval 600
echo Izleyici cikti. Kod: %ERRORLEVEL%
pause
