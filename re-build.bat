@echo off
REM Disable sleep (AC plugged in, DC battery)
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0 

echo Running Docker build...
docker build -t --no-cache whisperx-container . 

REM Re-enable original sleep settings (assumes common defaults like 20-30 min; adjust if needed)
powercfg /change standby-timeout-ac 30
powercfg /change standby-timeout-dc 15
powercfg /change monitor-timeout-ac 15
powercfg /change monitor-timeout-dc 5
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0 

echo Build complete. Sleep settings restored.
pause
