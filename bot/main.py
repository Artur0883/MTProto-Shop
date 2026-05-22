import logging
import signal
import time

from config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    running = True

    def stop(signum: int, _frame: object) -> None:
        nonlocal running
        logging.info("Received signal %s, stopping minimal bot container", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logging.info("Minimal bot container is running")
    logging.info("Proxy config path: %s", settings.proxy_config_path)
    logging.info("Server host: %s, proxy port: %s", settings.server_host, settings.proxy_port)

    while running:
        time.sleep(30)


if __name__ == "__main__":
    main()
