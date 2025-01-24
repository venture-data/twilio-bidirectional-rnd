from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)

def parse_time_to_utc_plus_5(time_str: str) -> str:
    """
    Parses a time string and converts it to UTC+5 timezone.

    Args:
        time_str (str): The time string to parse.

    Returns:
        str: ISO-formatted time string in UTC+5.
    """
    try:
        dt = datetime.fromisoformat(time_str)
        utc_plus_5 = timezone(timedelta(hours=5))
        dt_converted = dt.astimezone(utc_plus_5)
        return dt_converted.isoformat()
    except Exception as e:
        logger.error(f"Error parsing time string '{time_str}': {e}")
        raise e
