import logging
import sys

import uvicorn

from wiki_mcp.app import create_app
from wiki_mcp.config import Config, ConfigError


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as e:
        sys.exit(str(e))
    uvicorn.run(create_app(config), host="0.0.0.0", port=config.port, log_level="info")


if __name__ == "__main__":
    main()
