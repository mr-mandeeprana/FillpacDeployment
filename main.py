import logging
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from src.application import Application

from dashboard.backend.server import (
    app as dashboard_app,
    register_live_pipeline,
)


class _IgnoreWinConnReset(logging.Filter):
    """Hide harmless WinError 10054 noise from asyncio on Windows."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, ConnectionResetError)


logging.getLogger("asyncio").addFilter(_IgnoreWinConnReset())
# ==========================================================
# MAIN LOGGER
# ==========================================================

logger = logging.getLogger(
    "fillpac.main"
)


# ==========================================================
# GLOBAL APPLICATION REFERENCE
#
# Used by signal handlers.
# ==========================================================

APPLICATION = None


# ==========================================================
# OPEN DASHBOARD IN BROWSER
# ==========================================================

def open_dashboard_when_ready(
    url="http://127.0.0.1:8000",
    timeout=60,
):
    """
    Open the dashboard in the default browser as soon as
    the server responds.

    Runs in its own daemon thread, so it never delays
    application startup.

    Any HTTP response (even 404) means the server is up.
    Only connection-level failures are retried.
    """

    def _wait_and_open():

        deadline = time.time() + timeout

        while time.time() < deadline:

            try:

                with urllib.request.urlopen(
                    url,
                    timeout=2,
                ) as response:

                    if response.status < 500:

                        webbrowser.open(url)

                        logger.info(
                            "Dashboard opened: %s",
                            url,
                        )

                        return


            except urllib.error.HTTPError as error:

                # Server answered with 4xx/5xx. urllib raises
                # for these, but a 4xx still proves the
                # server is up.
                if error.code < 500:

                    webbrowser.open(url)

                    logger.info(
                        "Dashboard opened: %s",
                        url,
                    )

                    return


            except Exception:

                # Connection refused / timeout: server not
                # ready yet.
                pass


            time.sleep(1)


        logger.error(
            "Dashboard did not become ready: %s",
            url,
        )


    threading.Thread(

        target=_wait_and_open,

        name="FillPacDashboardOpener",

        daemon=True,

    ).start()


# ==========================================================
# DASHBOARD SERVER
# ==========================================================

class DashboardServer:
    """
    Runs Uvicorn inside a background thread.

    The dashboard remains in the same Python process as
    FillPac AI so LIVE_PIPELINES can contain actual Pipeline
    instances.
    """

    def __init__(
        self,
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    ):

        self.app = app

        self.host = str(
            host
        )

        self.port = int(
            port
        )

        self.log_level = str(
            log_level
        ).lower()


        self.thread = None

        self.server = None

        self.started = False


    # ======================================================
    # START
    # ======================================================

    def start(
        self,
    ):
        """
        Start dashboard server in background thread.
        """

        if (
            self.thread is not None
            and
            self.thread.is_alive()
        ):

            return


        config = uvicorn.Config(

            app=self.app,

            host=self.host,

            port=self.port,

            log_level=self.log_level,

            access_log=False,

            # ------------------------------------------------
            # IMPORTANT
            #
            # workers must remain 1.
            #
            # Multiple workers would create multiple process
            # memories and break LIVE_PIPELINES sharing.
            # ------------------------------------------------

            workers=1,
        )


        self.server = uvicorn.Server(
            config
        )


        self.thread = threading.Thread(

            target=self._run,

            name="FillPacDashboardServer",

            daemon=True,
        )


        self.thread.start()


        # --------------------------------------------------
        # Wait briefly for Uvicorn startup.
        # --------------------------------------------------

        timeout = 10.0

        start_time = time.monotonic()


        while (
            not self.server.started
            and
            self.thread.is_alive()
        ):

            if (
                time.monotonic()
                -
                start_time
                >
                timeout
            ):

                raise RuntimeError(
                    "Dashboard server startup timed out."
                )


            time.sleep(
                0.05
            )


        if not self.thread.is_alive():

            raise RuntimeError(
                "Dashboard server stopped during startup."
            )


        self.started = True


    # ======================================================
    # INTERNAL RUN
    # ======================================================

    def _run(
        self,
    ):

        try:

            self.server.run()


        except Exception:

            logger.exception(
                "Dashboard server crashed."
            )


        finally:

            self.started = False


    # ======================================================
    # STOP
    # ======================================================

    def stop(
        self,
        timeout=5.0,
    ):
        """
        Gracefully stop Uvicorn.
        """

        if self.server is None:
            return


        try:

            self.server.should_exit = True


            if (
                self.thread is not None
                and
                self.thread.is_alive()
                and
                self.thread
                is not
                threading.current_thread()
            ):

                self.thread.join(
                    timeout=timeout
                )


                if self.thread.is_alive():

                    logger.warning(
                        "Dashboard server did not stop "
                        "within timeout."
                    )


        except Exception:

            logger.exception(
                "Dashboard server shutdown failed."
            )


        finally:

            self.started = False


    # ======================================================
    # STATUS
    # ======================================================

    def is_running(
        self,
    ):

        return bool(

            self.started

            and

            self.thread is not None

            and

            self.thread.is_alive()
        )


# ==========================================================
# REGISTER LIVE PIPELINES
# ==========================================================

def register_application_pipelines(
    application,
):
    """
    Register every Pipeline created by Application with
    dashboard.backend.server.

    This enables:

        GET /live/Camera 1
        GET /live/Camera 2
        GET /live/Camera 3
        GET /live/Camera 4

    Each endpoint obtains the latest annotated frame using:

        pipeline.get_latest_frame()
    """

    registered = 0


    for pipeline in application.pipelines:

        try:

            register_live_pipeline(
                pipeline.name,
                pipeline,
            )


            registered += 1


            application.logger.info(
                f"{pipeline.name} registered "
                "with dashboard Live Monitor."
            )


        except Exception as error:

            application.logger.warning(
                f"{pipeline.name} could not be "
                f"registered with dashboard: {error}"
            )


    return registered


# ==========================================================
# SIGNAL HANDLER
# ==========================================================

def handle_shutdown_signal(
    signum,
    frame,
):
    """
    Handle CTRL+C / operating-system termination.
    """

    global APPLICATION


    logger.info(
        "Shutdown signal received: %s",
        signum,
    )


    if APPLICATION is not None:

        try:

            APPLICATION.stop_event.set()

        except Exception:

            pass


# ==========================================================
# MAIN
# ==========================================================

def main():

    global APPLICATION


    dashboard_server = None


    # ======================================================
    # REGISTER SIGNALS
    # ======================================================

    try:

        signal.signal(
            signal.SIGINT,
            handle_shutdown_signal,
        )


        signal.signal(
            signal.SIGTERM,
            handle_shutdown_signal,
        )


    except (
        ValueError,
        AttributeError,
    ):

        # Signal registration may fail if main() is executed
        # outside the main interpreter thread.
        pass


    # ======================================================
    # START APPLICATION
    # ======================================================

    try:

        print()
        print(
            "================================================"
        )
        print(
            "                 FILLPAC AI"
        )
        print(
            "      Production Vision System Starting"
        )
        print(
            "================================================"
        )
        print()


        # ==================================================
        # INITIALIZE APPLICATION
        #
        # This creates:
        #
        # - DashboardState
        # - CountLogger
        # - Elasticsearch
        # - Detector
        # - InferenceManager
        # - Camera Pipelines
        # ==================================================

        APPLICATION = Application()


        # ==================================================
        # APPLICATION LOGGER
        # ==================================================

        app_logger = APPLICATION.logger


        app_logger.info(
            "Main application initialized."
        )


        # ==================================================
        # DASHBOARD CONFIG
        # ==================================================

        dashboard_config = (

            APPLICATION.config.get(
                "dashboard",
                default={},
            )

            or {}
        )


        dashboard_enabled = bool(

            dashboard_config.get(
                "enabled",
                True,
            )
        )


        dashboard_host = (

            dashboard_config.get(
                "host",
                "0.0.0.0",
            )
        )


        dashboard_port = int(

            dashboard_config.get(
                "port",
                8000,
            )
        )


        # ==================================================
        # REGISTER LIVE PIPELINES
        #
        # Must happen before dashboard requests start
        # arriving.
        # ==================================================

        if dashboard_enabled:

            registered = (
                register_application_pipelines(
                    APPLICATION
                )
            )


            app_logger.info(
                f"{registered} live pipeline(s) "
                "registered with dashboard."
            )


        # ==================================================
        # START DASHBOARD
        # ==================================================

        if dashboard_enabled:

            dashboard_server = DashboardServer(

                app=dashboard_app,

                host=dashboard_host,

                port=dashboard_port,

                log_level="info",
            )


            dashboard_server.start()


            app_logger.info(
                "Dashboard server started."
            )


            app_logger.info(
                f"Dashboard port: {dashboard_port}"
            )


            # ----------------------------------------------
            # Open the browser automatically once the
            # dashboard responds. Uses the real configured
            # port, not a hardcoded one.
            # ----------------------------------------------

            open_dashboard_when_ready(
                f"http://127.0.0.1:{dashboard_port}"
            )


            print()
            print(
                "Dashboard:"
            )

            print(
                f"  http://127.0.0.1:{dashboard_port}"
            )

            print()

            print(
                "Live Monitor:"
            )

            print(
                f"  http://127.0.0.1:"
                f"{dashboard_port}/live/Camera%201"
            )

            print(
                f"  http://127.0.0.1:"
                f"{dashboard_port}/live/Camera%202"
            )

            print(
                f"  http://127.0.0.1:"
                f"{dashboard_port}/live/Camera%203"
            )

            print(
                f"  http://127.0.0.1:"
                f"{dashboard_port}/live/Camera%204"
            )

            print()


        else:

            app_logger.info(
                "Dashboard disabled by configuration."
            )


        # ==================================================
        # RUN AI APPLICATION
        #
        # Application.run() starts camera pipeline threads
        # and keeps the main thread alive.
        # ==================================================

        APPLICATION.run()


    # ======================================================
    # KEYBOARD INTERRUPT
    # ======================================================

    except KeyboardInterrupt:

        if APPLICATION is not None:

            try:

                APPLICATION.logger.info(
                    "Keyboard interrupt received in main."
                )

            except Exception:

                pass


            try:

                APPLICATION.stop_event.set()

            except Exception:

                pass


    # ======================================================
    # FATAL ERROR
    # ======================================================

    except Exception as error:

        print()
        print(
            "================================================"
        )
        print(
            "FILLPAC AI STARTUP ERROR"
        )
        print(
            "================================================"
        )

        print(
            str(
                error
            )
        )

        print()


        traceback.print_exc()


        if APPLICATION is not None:

            try:

                APPLICATION.logger.error(
                    "Fatal application error: "
                    f"{error}"
                )

            except Exception:

                pass


    # ======================================================
    # FINAL SHUTDOWN
    # ======================================================

    finally:

        # --------------------------------------------------
        # Stop AI application first.
        #
        # Application.stop() is idempotent, so this is safe
        # even when Application.run() already called stop().
        # --------------------------------------------------

        if APPLICATION is not None:

            try:

                APPLICATION.stop()

            except Exception:

                logger.exception(
                    "Application shutdown failed."
                )


        # --------------------------------------------------
        # Stop dashboard server.
        # --------------------------------------------------

        if dashboard_server is not None:

            try:

                dashboard_server.stop()

            except Exception:

                logger.exception(
                    "Dashboard shutdown failed."
                )


        print()
        print(
            "================================================"
        )
        print(
            "             FILLPAC AI STOPPED"
        )
        print(
            "================================================"
        )
        print()


# ==========================================================
# ENTRY POINT
# ==========================================================

if __name__ == "__main__":

    main()