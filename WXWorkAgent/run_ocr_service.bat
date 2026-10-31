@echo off
cd /d %~dp0
set GPU_PY=D:\Workspace\.venv-wxocr-gpu\Scripts\python.exe
if exist "%GPU_PY%" (
  set "NVIDIA_SITE=D:\Workspace\.venv-wxocr-gpu\Lib\site-packages\nvidia"
  set "PATH=%NVIDIA_SITE%\cudnn\bin;%NVIDIA_SITE%\cublas\bin;%NVIDIA_SITE%\cuda_nvrtc\bin;%PATH%"
  "%GPU_PY%" wxwork_ocr_service.py --backend paddle --device gpu:0 --max-side 960
) else (
  py -3.11 wxwork_ocr_service.py --backend rapidocr --max-side 480
)
