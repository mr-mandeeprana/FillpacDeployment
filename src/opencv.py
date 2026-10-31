import cv2

url = "rtsp://BEUMER:Beumer!123@169.254.49.188:554/live/1/1"

options = "rtsp_transport;tcp|timeout;5000000"
cap = cv2.VideoCapture()

cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

# Set TCP transport before opening
import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options

cap.open(url, cv2.CAP_FFMPEG)

if not cap.isOpened():
    print("Cannot open RTSP stream. Check URL, network and camera settings.")
else:
    print("Connected to RTSP camera")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame")
            break

        cv2.imshow("RTSP Camera", frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

cap.release()
cv2.destroyAllWindows()