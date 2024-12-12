import logging
import os

# Set the path for the log file
LOG_FILE_PATH = os.path.join(os.path.dirname(__file__), "twilio_inbound_stream.log")

def get_logger(name: str) -> logging.Logger:
    """
    Configure and return a logger with a FileHandler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Avoid adding multiple handlers if this is called multiple times
    if not logger.handlers:
        # Create file handler
        fh = logging.FileHandler(LOG_FILE_PATH)
        fh.setLevel(logging.DEBUG)

        # Create console handler if you want logs on stdout as well
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)

        # Create formatter
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

        # Add formatter to handlers
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)

        # Add handle
