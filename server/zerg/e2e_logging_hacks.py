"""
Helper: Aggressively silence all logger.info() logs when called.
Only imported/used during E2E runs where log cleanliness is essential.
"""


def silence_info_logs():
    import logging

    def noop_info(self, *args, **kwargs):
        pass

    logging.Logger.info = noop_info
