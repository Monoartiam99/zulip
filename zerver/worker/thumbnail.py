import logging
import time
from dataclasses import asdict
from io import BytesIO
from typing import Any

import pyvips
from django.db import transaction
from typing_extensions import override

from zerver.lib.mime_types import guess_type
from zerver.lib.thumbnail import StoredThumbnailFormat, missing_thumbnails
from zerver.lib.upload import get_image_thumbnail_path, save_attachment_contents, upload_backend
from zerver.models import ImageAttachment
from zerver.worker.base import QueueProcessingWorker, assign_queue

logger = logging.getLogger(__name__)


@assign_queue("thumbnail")
class ThumbnailWorker(QueueProcessingWorker):
    @override
    def consume(self, event: dict[str, Any]) -> None:
        start = time.time()
        with transaction.atomic(savepoint=False):
            try:
                row = ImageAttachment.objects.select_for_update().get(id=event["id"])
            except ImageAttachment.DoesNotExist:  # nocoverage
                logger.info("ImageAttachment row %d missing", event["id"])
                return
            uploaded_thumbnails = ensure_thumbnails(row)
        end = time.time()
        logger.info(
            "Processed %d thumbnails (%dms)",
            uploaded_thumbnails,
            (end - start) * 1000,
        )


def ensure_thumbnails(image_attachment: ImageAttachment) -> int:
    needed_thumbnails = missing_thumbnails(image_attachment)

    if not needed_thumbnails:
        return 0

    written_images = 0
    image_bytes = BytesIO()
    save_attachment_contents(image_attachment.path_id, image_bytes)
    try:
        # TODO: We could save some computational time by using the same
        # bytes if multiple resolutions are larger than the source
        # image.  That is, if the input is 10x10, a 100x100.jpg is
        # going to be the same as a 200x200.jpg, since those set the
        # max dimensions, and we do not scale up.
        for thumbnail_format in needed_thumbnails:
            # This will scale to fit within the given dimensions; it
            # may be smaller one one or more of them.
            logger.info(
                "Resizing to %d x %d, from %d x %d",
                thumbnail_format.max_width,
                thumbnail_format.max_height,
                image_attachment.original_width_px,
                image_attachment.original_height_px,
            )
            load_opts = ""
            if image_attachment.frames > 1:
                # If the original has multiple frames, we want to load
                # one of them if we're outputting to a static format,
                # otherwise we load them all.
                if thumbnail_format.animated:
                    load_opts = "n=-1"
                else:
                    load_opts = "n=1"
            resized = pyvips.Image.thumbnail_buffer(
                image_bytes.getbuffer(),
                thumbnail_format.max_width,
                height=thumbnail_format.max_height,
                option_string=load_opts,
                size=pyvips.Size.DOWN,
            )
            thumbnailed_bytes = resized.write_to_buffer(
                f".{thumbnail_format.extension}[{thumbnail_format.opts}]"
            )
            content_type = guess_type(f"image.{thumbnail_format.extension}")[0]
            assert content_type is not None
            thumbnail_path = get_image_thumbnail_path(image_attachment, thumbnail_format)
            logger.info("Uploading %d bytes to %s", len(thumbnailed_bytes), thumbnail_path)
            upload_backend.upload_message_attachment(
                thumbnail_path,
                content_type,
                thumbnailed_bytes,
                None,
            )
            height = resized.get("page-height") if thumbnail_format.animated else resized.height
            image_attachment.thumbnail_metadata.append(
                asdict(
                    StoredThumbnailFormat(
                        extension=thumbnail_format.extension,
                        content_type=content_type,
                        max_width=thumbnail_format.max_width,
                        max_height=thumbnail_format.max_height,
                        animated=thumbnail_format.animated,
                        width=resized.width,
                        height=height,
                        byte_size=len(thumbnailed_bytes),
                    )
                )
            )
            written_images += 1

    except pyvips.Error as e:
        logger.exception(e)

        if written_images == 0 and len(image_attachment.thumbnail_metadata) == 0:
            # We have never thumbnailed this -- it most likely had
            # bad data.  Remove the ImageAttachment row, since it is
            # not valid for thumbnailing.
            image_attachment.delete()
            return 0

    image_attachment.save(update_fields=["thumbnail_metadata"])

    return written_images
