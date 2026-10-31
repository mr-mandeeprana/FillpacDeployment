# Dashboard Frontend

This folder is reserved for the live monitoring frontend.

Expected widgets:

- live camera streams
- total bag count
- camera-wise count
- print status
- fps
- camera health
- system status

## Camera Health Details

The dashboard should surface camera health metadata from the backend `state` payload under each camera's `camera_status` object.

Expected fields:

- `connected`: whether the camera capture thread is active
- `backend`: capture backend in use (for RTSP, `ffmpeg`)
- `queue_size`: configured capture buffer queue size
- `queue_occupancy`: number of frames currently queued for processing
- `frames_read`: total frames read from the source
- `frames_dropped`: frames dropped when the queue was full
- `last_frame_age_seconds`: age of the most recent frame in seconds
