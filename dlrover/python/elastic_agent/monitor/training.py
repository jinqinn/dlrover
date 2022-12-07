# Copyright 2022 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import time

from dlrover.python.common.log import default_logger as logger
from dlrover.python.common.singleton import singleton
from dlrover.python.elastic_agent.master_client import GlobalMasterClient
from dlrover.python.elastic_agent.monitor.resource import ResourceMonitor


def is_tf_chief():
    TF_CONFIG = os.getenv("TF_CONFIG", "")
    if not TF_CONFIG:
        return False
    config = json.loads(TF_CONFIG)
    task = config.get("task", None)
    if not task:
        return False
    if task.get("type", None) == "chief" and task.get("index", None) == 0:
        return True
    return False


@singleton
class TrainingProcessReporter(object):
    def __init__(self):
        self._resource_monitor = ResourceMonitor()
        self._last_timestamp = 0
        self._start_time = 0
        self.called_in_tf_hook = False
        self._is_tf_chief = is_tf_chief()

    def set_start_time(self):
        if self._start_time == 0:
            timestamp = int(time.time())
            self._last_timestamp = timestamp
            self._start_time = timestamp
            self._resource_monitor.start_monitor_cpu()
            logger.info(
                "Start training process reporter in TF hooks : %s",
                self.called_in_tf_hook,
            )

    def report_resource_with_step(self, step):
        if not self._is_tf_chief:
            return
        try:
            timestamp = int(time.time())
            secs = self.get_wait_seconds(timestamp)
            if step > 0 and timestamp - self._last_timestamp > secs:
                self._resource_monitor.report_resource()
                logger.info("Report global step = {}".format(step))
                self._last_timestamp = timestamp
                GlobalMasterClient.MASTER_CLIENT.report_global_step(
                    step, self._last_timestamp
                )
        except Exception as e:
            logger.warning(e)

    def get_wait_seconds(self, timestamp):
        """Adjust the waited seconds by the training time"""
        if timestamp - self._start_time < 1800:
            return 60
        elif timestamp - self._start_time < 3600:
            return 180
        else:
            return 300