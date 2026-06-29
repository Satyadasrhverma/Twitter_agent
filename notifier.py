"""
Windows desktop toast notifications via winotify.
Clicking the toast opens the post URL in the default browser.
"""

import logging
import webbrowser
from datetime import datetime

from winotify import Notification, audio

import config
from models import PostInfo

logger = logging.getLogger(__name__)

class ToastNotifier:
    """Sends Windows 11 toast notifications for new X posts."""

    def __init__(self) -> None:
        self._app_id = config.NOTIFICATION_APP_NAME

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, post: PostInfo) -> None:
        """
        Display a Windows toast notification for *post*.
        Clicking the notification opens post.post_url in the default browser.
        Failures are logged but never raised — a broken notification must
        never crash the monitoring loop.
        """
        try:
            self._show_toast(post)
            logger.info(
                "Notification sent — @%s  post_id=%s",
                post.username,
                post.post_id,
            )
        except Exception as exc:
            logger.error("Failed to send notification for @%s: %s", post.username, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _show_toast(self, post: PostInfo) -> None:
        """Build and display the toast."""
        name = post.display_name or f"@{post.username}"
        title = f"New post from {name}"
        body = self._build_body(post)

        toast = Notification(
            app_id=self._app_id,
            title=title,
            msg=body,
            duration=config.NOTIFICATION_DURATION,
            # icon= winotify supports a local .ico path; skip remote URL
        )

        # Open the post URL in the default browser when clicked
        toast.add_actions(
            label="Open Post",
            launch=post.post_url,
        )

        if config.NOTIFICATION_SOUND:
            toast.set_audio(audio.Default, loop=False)
        else:
            toast.set_audio(audio.Silent, loop=False)

        toast.show()

    def _build_body(self, post: PostInfo) -> str:
        """Compose the two-line notification body."""
        time_str = post.detected_at.strftime("%H:%M:%S")
        handle = f"@{post.username}"
        lines = [
            handle,
            f"Detected at {time_str}",
        ]
        return "\n".join(lines)
