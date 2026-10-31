"""
==========================================================
FillPac AI
Elasticsearch Module
==========================================================
"""

import time
import queue
import threading
from datetime import datetime

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ConnectionError, TransportError


class ElasticSearch:
    def __init__(
        self,
        host,
        port,
        indices=None,
        enabled=True,
        logger=None,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ):
        self.enabled = enabled
        self.logger = logger
        self.indices = indices or {
            "count": "fillpac-count",
            "print": "fillpac-print",
            "camera": "fillpac-camera",
        }
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.host = host
        self.port = port
        self.client = None
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = None

        if self.enabled:
            self._connect(self.host, self.port)
            self._start_worker()

    def _start_worker(self):
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="ElasticSearchWorker",
        )
        self._worker.start()

    def _connect(self, host, port):
        self.client = Elasticsearch(
            f"http://{host}:{port}",
            retry_on_timeout=True,
            max_retries=self.max_retries,
        )
        try:
            if self.client.ping():
                self._log("info", "Elasticsearch connected")
            else:
                raise ConnectionError("Ping failed")
        except Exception as error:
            self._log("warning", f"Elasticsearch connection failed: {error}")
            self.client = None

    def is_connected(self):
        if not self.enabled or self.client is None:
            return False

        try:
            return self.client.ping()
        except Exception:
            return False

    def send(self, index_name: str, data: dict):
        if not self.enabled:
            return

        self._queue.put({"index": index_name, "data": data})

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is None:
                break

            self._index_with_retries(item["index"], item["data"])
            self._queue.task_done()

        while not self._queue.empty():
            item = self._queue.get()
            if item is None:
                break
            self._index_with_retries(item["index"], item["data"])
            self._queue.task_done()

    def _index_with_retries(self, index_name: str, data: dict):
        retry_count = 0
        while retry_count <= self.max_retries:
            if self.client is None:
                self._connect(self.host, self.port)

            if self.client is None:
                retry_count += 1
                time.sleep(self.backoff_factor * retry_count)
                continue

            try:
                self.client.index(index=self.indices[index_name], document=data)
                return
            except (ConnectionError, TransportError) as error:
                retry_count += 1
                self._log(
                    "warning",
                    f"Elasticsearch index attempt {retry_count}/{self.max_retries} failed: {error}",
                )
                self._connect(self.host, self.port)
                time.sleep(self.backoff_factor * retry_count)
            except Exception as error:
                self._log("error", f"Elasticsearch indexing failed: {error}")
                return

    def close(self):
        self._stop_event.set()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)

    def create_count_event(self, camera, count, center):
        document = {
            "timestamp": datetime.now().isoformat(),
            "event": "bag_count",
            "camera": camera,
            "count": count,
            "center_x": center[0],
            "center_y": center[1],
        }
        self.send("count", document)

    def create_print_event(self, camera, status):
        document = {
            "timestamp": datetime.now().isoformat(),
            "event": "print_detection",
            "camera": camera,
            "print_present": status,
        }
        self.send("print", document)

    def create_camera_event(self, camera, fps, status):
        document = {
            "timestamp": datetime.now().isoformat(),
            "event": "camera_status",
            "camera": camera,
            "fps": fps,
            "status": status,
        }
        self.send("camera", document)

    def _log(self, level, message):
        if self.logger is not None:
            getattr(self.logger, level)(message)
            return

        print(f"[{level.upper()}] {message}")