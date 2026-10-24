@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "APP_DIR=%CD%"
title GameHub v1.7.0 - Criar executavel

echo ===========================================
echo   GAMEHUB v1.7.0 - GERAR EXECUTAVEL
echo ===========================================
echo.
echo Pasta do projeto: "%APP_DIR%"
echo.

if not exist "%APP_DIR%\GameHub.pyw" (
  echo ERRO: GameHub.pyw nao encontrado nesta pasta.
  goto :erro
)
if not exist "%APP_DIR%\GameHub.ico" (
  echo ERRO: GameHub.ico nao encontrado nesta pasta.
  goto :erro
)
if not exist "%APP_DIR%\requirements.txt" (
  echo ERRO: requirements.txt nao encontrado nesta pasta.
  goto :erro
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERRO: Python nao encontrado. Instale Python 3 antes.
        goto :erro
    )
    set "PY=python"
)

echo Instalando bibliotecas, se necessario...
%PY% -m pip install -r "%APP_DIR%\requirements.txt" pyinstaller
if errorlevel 1 goto :erro

rem Usa uma pasta temporaria para nao encher a pasta do GameHub.
set "TEMP_BUILD=%TEMP%\GameHub_Build_%RANDOM%_%RANDOM%"
mkdir "%TEMP_BUILD%" >nul 2>nul
if errorlevel 1 goto :erro

echo.
echo Verificacao: icone encontrado em "%APP_DIR%\GameHub.ico"
echo Criando GameHub.exe na mesma pasta do BAT...
echo.

rem O .spec fica ao lado do icone durante a compilacao e e apagado no final.
rem Os caminhos completos evitam problemas com nomes de pasta que tenham espacos.
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name GameHub --icon "%APP_DIR%\GameHub.ico" --add-data "%APP_DIR%\GameHub.ico;." --distpath "%APP_DIR%" --workpath "%TEMP_BUILD%" --specpath "%APP_DIR%" "%APP_DIR%\GameHub.pyw"
set "RESULT=%ERRORLEVEL%"

rem Apaga o .spec e os arquivos temporarios quando terminar.
if exist "%APP_DIR%\GameHub.spec" del /f /q "%APP_DIR%\GameHub.spec" >nul 2>nul
if exist "%TEMP_BUILD%" rmdir /s /q "%TEMP_BUILD%" >nul 2>nul

if not "%RESULT%"=="0" goto :erro
if not exist "%APP_DIR%\GameHub.exe" (
    echo ERRO: a compilacao terminou, mas GameHub.exe nao foi encontrado.
    goto :erro
)

echo.
echo ===========================================
echo SUCESSO! GameHub.exe criado nesta pasta:
echo "%APP_DIR%\GameHub.exe"
echo Agora abra o EXE com dois cliques, sem CMD.
echo ===========================================
pause
exit /b 0

:erro
echo.
echo ERRO: Nao foi possivel criar o GameHub.exe.
echo Confira a primeira mensagem de ERROR acima.
pause
exit /b 1
