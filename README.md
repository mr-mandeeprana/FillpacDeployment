# FillPac AI

**Industrial Computer Vision for Cement Bag Counting and Print Presence Inspection**

## 1. Project Overview

FillPac AI is an industrial computer vision system designed for cement bag counting and print presence inspection in a BEUMER Group FillPac packing plant.

The system uses a single YOLO26n model to detect bags and their print regions. ByteTrack provides detection stability, while center-based counting determines when bags cross the configured counting region.

The application supports multiple cameras, recorded video testing, live RTSP streams, real-time visualization, dashboard monitoring, and optional Elasticsearch event storage.

## 2. Key Features

- **Single YOLO26n model:** Detects Bag and Print classes.
- **Center-based bag counting:** Counts bags based on bounding-box center crossing logic.
- **Print presence verification:** Identifies whether a bag has a detected print region without performing OCR.
- **Multi-camera support:** Supports MP4 video files and RTSP camera streams.
- **ByteTrack integration:** Improves detection stability across consecutive frames.
- **GPU-accelerated inference:** Supports CUDA and FP16 inference where compatible.
- **FPS-oriented configuration:** Provides controls for inference image size, detection limits, and camera buffering.
- **Real-time visualization:** Displays detections, counting information, and configured inspection regions.
- **Dashboard backend:** Provides APIs and live monitoring through the web dashboard.
- **Elasticsearch integration:** Supports event storage using configurable count, print, and camera indices.
- **Jam monitoring:** Supports configured conveyor jam monitoring and ROI occupancy detection.
- **Centralized configuration:** Uses `config.yaml` to manage model, camera, tracking, counting, inspection, and dashboard settings.
- **Automated testing:** Supports project tests through pytest.

## 3. System Architecture

### Runtime Flow

```text
Camera / RTSP / MP4
        |
        v
Frame Capture
        |
        v
YOLO26n Detection
        |
        v
ByteTrack
        |
        v
+--------------------------+
|                          |
v                          v
Center-Based Counter   Print Detector
|                          |
+------------+-------------+
             |
             v
       Visualization
             |
             v
       Dashboard API
             |
       +-----+------+
       |            |
       v            v
   SQL Server   Elasticsearch
   (configured   (optional
   dashboard     event storage)
   data)
```

The diagram shows the intended integration options. The actual database and Elasticsearch behavior depends on the enabled configuration and implemented application modules.

### Counting Principle

ByteTrack is used for detection stability only. Counting is based on bounding-box centers crossing the configured counting region and does not depend on track IDs.

The counting logic uses configuration values such as:

- Counting method
- Counting direction
- Duplicate distance and time
- Line tolerance
- Minimum track frames
- Minimum crossing distance

### Print Detection Principle

The model detects the bag and print classes. The print detector uses the configured confidence, overlap, observation, and voting thresholds to determine print presence.

The system performs **print presence verification, not OCR**. It does not extract or validate printed text.

## 4. Camera Configuration

The intended deployment uses four cameras.

| Camera | Location | Function |
|---|---|---|
| Camera 1 | FillPac Opening 1 | Bag counting and print detection |
| Camera 2 | FillPac Opening 2 | Bag counting and print detection |
| Camera 3 | Truck Loading Conveyor 1 | Bag counting only |
| Camera 4 | Truck Loading Conveyor 2 | Bag counting only |

Camera enablement, source selection, capture buffering, inference settings, and other camera-specific behavior are controlled through `config.yaml`.

### Supported Sources

**Recorded video**

```yaml
cameras:
  - id: 1
    name: Camera 1
    enabled: true
    mode: video
    source: data/videos/camera1_test.mp4
    buffer_size: 1
```

**RTSP stream**

```yaml
cameras:
  - id: 1
    name: Camera 1
    enabled: true
    mode: rtsp
    source: "${CAMERA_1_RTSP}"
    buffer_size: 1
```

The RTSP example uses an environment-variable placeholder. Confirm that the application resolves environment variables in camera sources before using this format. Otherwise, provide the source through the configuration mechanism already supported by the project.

For production deployments, keep camera credentials outside version-controlled files.

## 5. Project Structure

FillPac_AI/
├── main.py
├── config.yaml
├── requirements.txt
├── README.md
├── train.py
├── run_model.bat
├── check_jam_table.py
├── extract_frames.py
├── database/
├── dashboard/
├── models/
├── data/
├── docs/
├── logs/
├── tests/
├── config_backups/
├── venv/
└── src/
    ├── application.py
    ├── config.py
    ├── config_manager.py
    ├── camera.py
    ├── detector.py
    ├── inference_manager.py
    ├── tracker.py
    ├── counter.py
    ├── print_detector.py
    ├── jam_detector.py
    ├── bag_spacing_detector.py
    ├── roi_occupancy_detector.py
    ├── pipeline.py
    ├── dashboard.py
    ├── elasticsearch.py
    ├── count_logger.py
    ├── visualizer.py
    ├── logger.py
    └── utils.py

This is a logical overview of the project components. The exact directory names and module locations should match the files present in the deployed repository.

### Important Files

| File or directory | Purpose |
|---|---|
| `main.py` | Main application entry point |
| `config.yaml` | Central runtime configuration |
| `requirements.txt` | Python dependencies |
| `models/` | Trained YOLO model files |
| `data/` | Test videos and dataset resources |
| `src/` | Camera processing and computer vision modules |
| `dashboard/backend/` | Dashboard APIs and server |
| `dashboard/frontend/` | Dashboard user interface |
| `database/` | Database connectivity and repository modules |
| `logs/` | Application logs and configured inspection snapshots |
| `tests/` | Automated tests |
| `docs/` | Technical documentation |

## 6. Requirements

### Software

- Python version compatible with the installed project dependencies
- PyTorch and Ultralytics-compatible YOLO dependencies
- OpenCV
- NumPy
- PyYAML
- PyODBC for SQL Server connectivity
- FastAPI and Uvicorn for the dashboard backend
- Socket.IO dependencies where required
- Elasticsearch client when Elasticsearch integration is enabled
- pytest for automated tests

The exact package versions should be maintained in `requirements.txt`.

### Hardware

**GPU inference**

A compatible NVIDIA GPU, CUDA-enabled PyTorch installation, and compatible drivers are required for CUDA inference and FP16 acceleration.

**CPU inference**

CPU inference is possible when configured and supported by the installed model runtime. Performance depends on the processor, image size, camera count, and inference workload.

### Model and Dataset Files

Expected files include:

```text
models/fillpac_yolo26n_best.pt
models/data.yaml
```

The trained model is required for inference. The dataset configuration is relevant to training and dataset validation.

## 7. Installation

Run commands from the project root directory.

### Step 1: Create a virtual environment

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use Command Prompt:

```bat
venv\Scripts\activate.bat
```

### Step 2: Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Step 3: Verify the model file

Confirm that the trained model exists:

```text
models/fillpac_yolo26n_best.pt
```

### Step 4: Verify the configuration

Check `config.yaml` for:

- Model path
- Inference device
- Image size
- Camera enablement and sources
- Counting configuration
- Print detection configuration
- Dashboard settings
- Database settings, if integrated
- Elasticsearch settings, if enabled

### Step 5: Start the application

```bash
python main.py
```

Use the project’s existing entry point and configuration conventions. If the application is structured as a Python package, use its documented module entry point where appropriate.

## 8. Configuration Reference

The main runtime settings are managed through `config.yaml`.

### Model

Example:

```yaml
model:
  path: models/fillpac_yolo26n_best.pt
  confidence: 0.2
  iou: 0.45
  device: cuda:0
  image_size: 640
  half: true
  max_detections: 20
  min_bbox_area: 800
```

| Setting | Purpose |
|---|---|
| `path` | Trained model location |
| `confidence` | Detection confidence threshold |
| `iou` | Non-maximum suppression IoU threshold |
| `device` | Inference device, such as `cuda:0` or `cpu` |
| `image_size` | Inference image size |
| `half` | Enables FP16 inference when supported |
| `max_detections` | Maximum detections returned per frame |
| `min_bbox_area` | Minimum bounding-box area filter |

Camera-specific model settings may override global model settings when the application supports them.

### Tracker

The `tracker` section configures ByteTrack behavior, including:

- High and low detection thresholds
- New-track threshold
- Track buffer
- Matching threshold
- Frame rate
- Minimum track age
- Bounding-box area filters
- Motion-jump and stability thresholds

These parameters influence tracking stability and the consistency of detections across frames.

### Counting

The `counting` section configures:

- Counting method
- Counting direction
- Duplicate suppression distance
- Duplicate suppression time
- Counting-line tolerance
- Minimum track frames
- Minimum crossing distance
- Stale-track handling

Use the physical camera orientation and actual bag movement direction to determine the appropriate counting direction.

### Print Detection

The `print_detection` section controls print inspection. Depending on the configured detector, it includes:

- Enablement
- Confidence and IoU thresholds
- Minimum overlap ratio
- Minimum print area
- Voting threshold
- Minimum votes and observations
- Observation history
- Motion-jump handling

Print detection verifies print presence; it does not read printed characters.

### Jam Detection

The `jam_detection` section configures conveyor monitoring, including:

- Detection ROI
- Motion history duration
- Stationary and slow-speed thresholds
- Warning and jam durations
- Recovery duration
- Minimum track age
- Unstable-track filtering
- Motion-jump filtering

The precise meaning of each parameter depends on the corresponding jam detector implementation.

### Condition C

The `condition_c` section supports ROI occupancy monitoring. Its configuration may include:

- Enablement
- ROI coordinates
- Maximum allowed bags
- Minimum track age
- Confirmation and recovery frame counts
- ROI image capture
- Distance information
- Image output directory

### Dashboard

Example:

```yaml
dashboard:
  enabled: true
  host: 0.0.0.0
  port: 8000
  publish_interval: 0.25
  stale_timeout: 10
```

The dashboard configuration controls server enablement, listening address, port, update publishing interval, and stale-state timeout.

## 9. Runtime FPS Tuning

FPS depends on the camera source, decoding, inference device, image size, detection count, and the number of simultaneously active cameras.

### Image Size

The `model.image_size` setting controls inference resolution.

- Lower image sizes generally improve inference speed.
- Higher image sizes may preserve smaller details but increase computation.
- Validate bag and print detection accuracy after every resolution change.

### FP16 Inference

The `model.half` setting enables half-precision inference when the hardware and inference runtime support it.

Use FP16 for compatible CUDA deployments. Do not assume that FP16 is supported or beneficial for every CPU or GPU configuration.

### Maximum Detections

The `model.max_detections` setting limits the number of detection results returned for a frame.

Choose a value that accommodates the maximum expected number of visible bags and print detections without unnecessary processing.

### Camera Buffering

The camera `buffer_size` setting influences frame buffering where supported by the capture implementation.

Small buffers can reduce latency for live streams, but their effect depends on the decoder, RTSP transport, camera configuration, and frame-processing pipeline.

### Recommended Tuning Procedure

1. Test one camera with the production model.
2. Measure FPS and end-to-end latency.
3. Confirm bag-count accuracy.
4. Confirm print-presence accuracy.
5. Adjust image size and detection limits.
6. Test camera buffering and dropped-frame behavior.
7. Enable additional cameras one at a time.
8. Verify total system throughput under the expected production workload.

Do not optimize FPS at the cost of missed bags, duplicate counts, or incorrect print inspection.

## 10. Dashboard Backend

The dashboard provides the web interface and API for system monitoring.

Start the backend from the project root using:

```bash
uvicorn dashboard.backend.app:app --host 0.0.0.0 --port 8000
```

The command assumes that `dashboard/backend/app.py` exports a FastAPI application named `app`.

If the actual server module exports a differently named application or uses another entry point, use the import path defined in that file.

When the dashboard and inference pipeline run as separate processes, confirm that the backend receives state and event updates through the project's configured integration mechanism.

Open the dashboard in a browser at:

```text
http://127.0.0.1:8000/
```

This address applies when the backend is running on the same machine and serves the frontend at `/`.

## 11. Database Integration

The project can use SQL Server for dashboard state, production events, jam events, user authentication, or other data according to the implemented database modules.

Database connection settings can be managed through the `database` section of `config.yaml`.

Example:

```yaml
database:
  enabled: true
  server: 'WS-7YHHYD3\SQLEXPRESS'
  name: FillPacAI
  driver: 'SQL Server'

  authentication:
    trusted_connection: true

  encrypt: false
  trust_server_certificate: true
  connect_timeout: 10
```

This example uses Windows integrated authentication. The Windows account running the Python application must have the required SQL Server permissions.

### Database Dependencies

Install the required packages through the project's dependency file. The connection module described here requires `pyodbc` and `PyYAML`.

### Database Verification

A database initialization function can verify the connection and selected database:

```python
from database.connection import initialize_database

initialize_database()
```

The database configuration, schema, tables, and repository methods must match the actual deployed application. Enabling a database connection does not automatically create every required table or implement authentication.

For production deployment, review SQL Server encryption and certificate settings with the responsible administrator before changing security settings.

## 12. Elasticsearch Event Storage

The project supports optional Elasticsearch event storage.

Configured index names:

| Index | Intended data |
|---|---|
| `fillpac-count` | Bag-count events |
| `fillpac-print` | Print inspection events |
| `fillpac-camera` | Camera-related events |

Example:

```yaml
elasticsearch:
  enabled: false
  host: localhost
  port: 9200
  indices:
    count: fillpac-count
    print: fillpac-print
    camera: fillpac-camera
```

Set `enabled: true` only after configuring the Elasticsearch service and confirming that the application's Elasticsearch integration is implemented and operational.

The index names shown above are configuration values; actual event documents and mappings depend on the indexing code.

## 13. Training

The project documentation expects a training entry point named `train.py`.

Example:

```bash
python train.py --epochs 50 --batch 8
```

This command applies only if `train.py` supports the `--epochs` and `--batch` arguments.

Before training:

1. Verify that the dataset exists.
2. Verify `models/data.yaml` or the dataset configuration path used by the training script.
3. Confirm the training and validation dataset paths.
4. Check the model architecture and installed framework version.
5. Select an appropriate batch size for available GPU memory.
6. Evaluate the trained model on representative factory images and videos.
7. Validate bag counting and print detection separately before production deployment.

Do not overwrite the production model until the replacement has been evaluated and approved.

## 14. Testing

Run the test suite from the project root:

```bash
python -m pytest -q
```

Useful validation areas include:

- Configuration loading and validation
- Model loading
- Video and RTSP capture
- Detection output
- ByteTrack stability
- Center-based counting
- Duplicate-count suppression
- Print presence verification
- Jam detection and recovery
- Dashboard API behavior
- Database connectivity and repository operations
- Elasticsearch event delivery

The actual coverage depends on the tests implemented in the repository.

## 15. Troubleshooting

### Model file not found

Check that the model path in `config.yaml` matches the actual model location.

### CUDA initialization failure

Verify NVIDIA driver installation, CUDA-compatible PyTorch packages, GPU availability, and the configured device. Use CPU mode only if the application supports it and CPU performance is acceptable.

### RTSP stream does not connect

Check camera reachability, RTSP URL, credentials, port, network routing, and camera streaming settings. Test the stream independently before debugging model inference.

### Low FPS or increasing latency

Check inference resolution, GPU utilization, camera buffering, decoding overhead, number of active cameras, and per-frame detection count. Measure inference time separately from capture and dashboard publishing.

### Duplicate or missed counts

Review counting direction, ROI coordinates, center-crossing logic, duplicate suppression thresholds, minimum track frames, and actual conveyor movement.

### Print detection errors

Review the trained model's class definitions, print confidence, overlap thresholds, minimum print area, and observation voting parameters.

### Dashboard unavailable

Confirm that the backend starts without exceptions, the configured port is available, and the frontend is served from the expected route. Check browser developer tools and backend logs for API errors.

### SQL Server connection failure

Verify the SQL Server service, instance name, database name, ODBC driver, Windows authentication settings, and permissions for the Windows account running the application.

### Elasticsearch events missing

Check that Elasticsearch is enabled, the service is reachable, the configured index names are correct, and the application logs do not contain indexing errors.

## 16. Logging

Application logging is configured through `config.yaml`.

Example:

```yaml
logging:
  level: INFO
  file: logs/application.log
  count_flush_immediately: true
```

Use logs to diagnose camera connection failures, model initialization, runtime exceptions, counting behavior, dashboard communication, and storage errors.

Avoid logging passwords, access tokens, or full connection strings containing credentials.

## 17. Production Deployment Checklist

Before deploying to a live packing plant, verify:

- [ ] Production model path and class mapping are correct.
- [ ] Camera sources and camera IDs are correct.
- [ ] Each camera has the correct counting and print-detection settings.
- [ ] Counting direction and ROI boundaries have been validated.
- [ ] Bag counts have been compared against manual ground truth.
- [ ] Print detection has been evaluated on printed and unprinted bags.
- [ ] Jam detection has been tested against representative operating conditions.
- [ ] GPU and camera performance are acceptable under full load.
- [ ] Dashboard APIs and frontend are working.
- [ ] SQL Server connectivity and permissions have been verified, if used.
- [ ] Elasticsearch delivery has been verified, if enabled.
- [ ] Logs and error handling are operational.
- [ ] Camera credentials and other secrets are not committed to source control.
- [ ] Configuration changes have been backed up and validated.
- [ ] A rollback plan exists for model and configuration updates.

## 18. Project Summary

FillPac AI combines industrial computer vision, bag counting, print presence inspection, multi-camera processing, tracking-assisted detection stability, dashboard monitoring, and configurable event storage.

The central design principles are:

1. Use a single YOLO26n model for bag and print detection.
2. Use ByteTrack for detection stability.
3. Count bags through bounding-box center crossing rather than track IDs.
4. Verify print presence without OCR.
5. Tune inference and capture settings for low-latency production operation.
6. Keep model, camera, counting, inspection, and integration settings centrally configurable.
7. Validate accuracy, performance, and system behavior before production deployment.
#   F i l l p a c D e p  
 