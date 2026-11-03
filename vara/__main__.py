import logging

import uvicorn

from vara.config import get_settings
from vara.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("vara").warning(
            "Binding to %s exposes the API beyond this computer; device authentication "
            "is not implemented yet.",
            settings.host,
        )
    uvicorn.run(
        "vara.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
