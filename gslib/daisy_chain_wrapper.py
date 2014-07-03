# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Wrapper for use in daisy-chained copies."""

from collections import deque
import os
import threading
import time

from gslib.cloud_api import BadRequestException
from gslib.cloud_api import CloudApi
from gslib.util import CreateLock
from gslib.util import TRANSFER_BUFFER_SIZE


# This controls the amount of bytes downloaded per download request.
# We do not buffer this many bytes in memory at a time - that is controlled by
# DaisyChainWrapper.max_buffer_size. This is the upper bound of bytes that may
# be unnecessarily downloaded if there is a break in the resumable upload.
DAISY_CHAIN_CHUNK_SIZE = 1024*1024*100


class BufferWrapper(object):
  """Wraps the download file pointer to use our in-memory buffer."""

  def __init__(self, daisy_chain_wrapper):
    """Provides a buffered write interface for a file download.

    Args:
      daisy_chain_wrapper: DaisyChainWrapper instance to use for buffer and
                           locking.
    """
    self.daisy_chain_wrapper = daisy_chain_wrapper

  def write(self, data):  # pylint: disable=invalid-name
    """Waits for space in the buffer, then writes data to the buffer."""
    while True:
      with self.daisy_chain_wrapper.lock:
        if (self.daisy_chain_wrapper.bytes_buffered <
            self.daisy_chain_wrapper.max_buffer_size):
          break
      # Buffer was full, yield thread priority so the upload can pull from it.
      time.sleep(0)
    data_len = len(data)
    with self.daisy_chain_wrapper.lock:
      self.daisy_chain_wrapper.buffer.append(data)
      self.daisy_chain_wrapper.bytes_buffered += data_len


class DaisyChainWrapper(object):
  """Wrapper class for daisy-chaining a cloud download to an upload.

  This class instantiates a BufferWrapper object to buffer the download into
  memory, consuming a maximum of max_buffer_size. It implements intelligent
  behavior around read and seek that allow for all of the operations necessary
  to copy a file.

  This class is coupled with the XML and JSON implementations in that it
  expects that small buffers (maximum of TRANSFER_BUFFER_SIZE) in size will be
  used.
  """

  def __init__(self, src_url, src_obj_size, gsutil_api):
    """Initializes the daisy chain wrapper.

    Args:
      src_url: Source CloudUrl to copy from.
      src_obj_size: Size of source object.
      gsutil_api: gsutil Cloud API to use for the copy.
    """
    # Current read position for the upload file pointer.
    self.position = 0
    self.buffer = deque()

    self.bytes_buffered = 0
    self.max_buffer_size = 1024 * 1024  # 1 MB

    # We save one buffer's worth of data as a special case for boto,
    # which seeks back one buffer and rereads to compute hashes. This is
    # unnecessary because we can just compare cloud hash digests at the end,
    # but it allows this to work without modfiying boto.
    self.last_position = 0
    self.last_data = None

    # Protects buffer, position, bytes_buffered, last_position, and last_data.
    self.lock = CreateLock()

    self.src_obj_size = src_obj_size
    self.src_url = src_url

    # This is safe to use the upload and download thread because the download
    # thread calls only GetObjectMedia, which creates a new HTTP connection
    # independent of gsutil_api. Thus, it will not share an HTTP connection
    # with the upload.
    self.gsutil_api = gsutil_api

    self.download_thread = None
    self.stop_download = threading.Event()
    self.StartDownloadThread()

  def StartDownloadThread(self, start_byte=0):
    """Starts the download thread for the source object (from start_byte)."""

    def PerformDownload(start_byte):
      """Downloads the source object in chunks.

      This function checks the stop_download event and exits early if it is set.
      It should be set when there is an error during the daisy-chain upload,
      then this function can be called again with the upload's current position
      as start_byte.

      Args:
        start_byte: Byte from which to begin the download.
      """
      # TODO: Support resumable downloads. This would require the BufferWrapper
      # object to support seek() and tell() which requires coordination with
      # the upload.
      while start_byte + DAISY_CHAIN_CHUNK_SIZE < self.src_obj_size:
        self.gsutil_api.GetObjectMedia(
            self.src_url.bucket_name, self.src_url.object_name,
            BufferWrapper(self), start_byte=start_byte,
            end_byte=start_byte + DAISY_CHAIN_CHUNK_SIZE - 1,
            generation=self.src_url.generation, object_size=self.src_obj_size,
            download_strategy=CloudApi.DownloadStrategy.ONE_SHOT,
            provider=self.src_url.scheme)
        if self.stop_download.is_set():
          # Download thread needs to be restarted, so exit.
          self.stop_download.clear()
          return
        start_byte += DAISY_CHAIN_CHUNK_SIZE
      self.gsutil_api.GetObjectMedia(
          self.src_url.bucket_name, self.src_url.object_name,
          BufferWrapper(self), start_byte=start_byte,
          generation=self.src_url.generation, object_size=self.src_obj_size,
          download_strategy=CloudApi.DownloadStrategy.ONE_SHOT,
          provider=self.src_url.scheme)

    # TODO: If we do gzip encoding transforms mid-transfer, this will fail.
    self.download_thread = threading.Thread(target=PerformDownload,
                                            args=(start_byte,))
    self.download_thread.start()

  def read(self, amt=None):  # pylint: disable=invalid-name
    """Exposes a stream from the in-memory buffer to the upload."""
    if self.position == self.src_obj_size or amt == 0:
      # If there is no data left or 0 bytes were requested, return an empty
      # string so callers can call still call len() and read(0).
      return ''
    if amt is None or amt > TRANSFER_BUFFER_SIZE:
      raise BadRequestException(
          'Invalid HTTP read size %s during daisy chain operation, '
          'expected <= %s.' % (amt, TRANSFER_BUFFER_SIZE))
    while True:
      with self.lock:
        if self.buffer:
          break
      # Buffer was empty, yield thread priority so the download thread can fill.
      time.sleep(0)
    with self.lock:
      data = self.buffer.popleft()
      self.last_position = self.position
      self.last_data = data
      data_len = len(data)
      self.position += data_len
      self.bytes_buffered -= data_len
    if data_len > amt:
      raise BadRequestException(
          'Invalid read during daisy chain operation, got data of size '
          '%s, expected size %s.' % (data_len, amt))
    return data

  def tell(self):  # pylint: disable=invalid-name
    with self.lock:
      return self.position

  def seek(self, offset, whence=os.SEEK_SET):  # pylint: disable=invalid-name
    restart_download = False
    if whence == os.SEEK_END:
      if offset:
        raise BadRequestException(
            'Invalid seek during daisy chain operation. Non-zero offset %s '
            'from os.SEEK_END is not supported' % offset)
      with self.lock:
        self.last_position = self.position
        self.last_data = None
        # Safe because we check position against src_obj_size in read.
        self.position = self.src_obj_size
    elif whence == os.SEEK_SET:
      with self.lock:
        if offset == self.position:
          pass
        elif offset == self.last_position:
          self.position = self.last_position
          if self.last_data:
            # If we seek to end and then back, we won't have last_data; we'll
            # get it on the next call to read.
            self.buffer.appendleft(self.last_data)
            self.bytes_buffered += len(self.last_data)
        else:
          # Once a download is complete, boto seeks to 0 and re-reads to
          # compute the hash if an md5 isn't already present (for example a GCS
          # composite object), so we have to re-download the whole object.
          # Also, when daisy-chaining to a resumable upload, on error the
          # service may have received any number of the bytes; the download
          # needs to be restarted from that point.
          restart_download = True

      if restart_download:
        self.stop_download.set()

        # Consume any remaining bytes in the download thread so that
        # the thread can exit, then restart the thread at the desired position.
        while self.download_thread.is_alive():
          with self.lock:
            while self.bytes_buffered:
              self.bytes_buffered -= len(self.buffer.popleft())
          time.sleep(0)

        with self.lock:
          self.position = offset
          self.buffer = deque()
          self.bytes_buffered = 0
          self.last_position = 0
          self.last_data = None
        self.StartDownloadThread(start_byte=offset)
    else:
      raise IOError('Daisy-chain download wrapper does not support '
                    'seek mode %s' % whence)

  def seekable(self):  # pylint: disable=invalid-name
    return True
