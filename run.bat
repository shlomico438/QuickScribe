@echo off
docker run --rm ^
  --env-file .env ^
  -v "C:\Work\runpod\QuickScribe\Audio:/data" ^
  -v "C:\Work\runpod\QuickScribe\Audio:/tmp/QuickScribeOutput" ^
  whisperx-container