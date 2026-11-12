@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\python.exe" (
    echo Missing portable runtime. Extract the entire Windows x64 ZIP first.
    pause
    exit /b 1
)
"runtime\python.exe" -I -B -c "import tkinter as tk, grpc, numpy, tifffile; from zen_api.lm.hardware.v2.focus_service_pb2_grpc import FocusServiceStub; from zen_api.acquisition.v1beta.experiment_streaming_service_pb2_grpc import ExperimentStreamingServiceStub; from zen_api.hardware.v1.stage_service_pb2_grpc import StageServiceStub; from zen_api.acquisition.v1beta.experiment_service_pb2_grpc import ExperimentServiceStub; from zen_api.lm.acquisition.v1.experiment_sw_autofocus_service_pb2_grpc import ExperimentSwAutofocusServiceStub; root=tk.Tk(); root.withdraw(); root.destroy(); print('PASS: Windows GUI and bundled Gateway dependencies')"
set "CHECK_EXIT=%ERRORLEVEL%"
if not "%CHECK_EXIT%"=="0" echo FAIL: Re-extract the complete ZIP on Windows x64.
if /i not "%~1"=="--no-pause" pause
exit /b %CHECK_EXIT%
