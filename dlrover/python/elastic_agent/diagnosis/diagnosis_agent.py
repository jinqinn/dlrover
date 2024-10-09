# Copyright 2024 The DLRover Authors. All rights reserved.
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
import threading
import time
from datetime import datetime
from typing import Dict

from torch.distributed.elastic.multiprocessing.errors import ProcessFailure

from dlrover.python.common import env_utils
from dlrover.python.common.constants import TrainingExceptionLevel
from dlrover.python.common.error import ProcessError
from dlrover.python.common.log import default_logger as logger
from dlrover.python.common.singleton import Singleton
from dlrover.python.common.worker import WorkerContext
from dlrover.python.diagnosis.common.constants import (
    DiagnosisAction,
    DiagnosisConstant,
    DiagnosisDataType,
    InferenceConfigKey,
)
from dlrover.python.diagnosis.common.diagnosis_data import WorkerTrainingMetric
from dlrover.python.diagnosis.common.inference_chain import (
    Inference,
    InferenceAttribute,
    InferenceDescription,
    InferenceName,
    is_inference_included,
)
from dlrover.python.diagnosis.datacollector.xpu_timer_metric_collector import (
    XpuTimerMetricsCollector,
)
from dlrover.python.diagnosis.inferencechain.inference_chain import (
    InferenceChain,
)
from dlrover.python.diagnosis.inferencechain.inferenceoperator.operator import (  # noqa: E501
    get_training_failure_operators,
)
from dlrover.python.elastic_agent.master_client import MasterClient


class DiagnosisAgent(Singleton):
    def __init__(self, training_log_file: str, errors: str):
        self._client = MasterClient.singleton_instance()
        self._training_log_file = training_log_file
        self._errors = errors
        self._xpu_timer_metric_collector = XpuTimerMetricsCollector()
        self._stopped = False

        self.start()

        logger.info(
            "Initializing diagnosis agent with\n"
            f"training_log_file:    {self._training_log_file}\n"
            f"errors:               {self._errors}"
        )

    def start(self):
        self._stopped = False

        # start a async thread to diagnose periodically
        thread = threading.Thread(
            target=self._periodically_diagnosis,
            name="periodically_diagnosis",
            daemon=True,
        )
        thread.start()

    def stop(self):
        self._stopped = True

    def _periodically_diagnosis(self):
        logger.info("Start periodically diagnosis...")
        while True:
            if self._stopped:
                logger.info("Stop periodically diagnosis.")
                break

            xpu_timer_metric = self._xpu_timer_metric_collector.collect_data()
            if xpu_timer_metric:
                agent_xpu_metric = WorkerTrainingMetric(
                    data_type=DiagnosisDataType.XPU_TIMER_METRIC,
                    data_content=xpu_timer_metric,
                    node_id=env_utils.get_node_id(),
                    node_type=env_utils.get_node_type(),
                    node_rank=env_utils.get_node_rank(),
                )
                self._report_metric_to_master(agent_xpu_metric)

            time.sleep(
                DiagnosisConstant.AGENT_PERIODICALLY_DIAGNOSIS_INTERVAL_SECS
            )

    def diagnose_training_failure(self, worker_context: WorkerContext) -> str:
        self._report_failure_to_master(
            worker_context.run_result.failures, worker_context.restart_count
        )
        # check if the node is failed
        inference = Inference(
            name=InferenceName.NODE,
            attribution=InferenceAttribute.ISORNOT,
            description=InferenceDescription.FAILURE,
            configs={
                InferenceConfigKey.LOG_FILE: self._training_log_file,
                InferenceConfigKey.ERRORS: self._errors,
            },
        )
        ic = InferenceChain([inference], get_training_failure_operators())
        infer_results = ic.infer()
        failure_inf = Inference(
            name=InferenceName.NODE,
            attribution=InferenceAttribute.IS,
            description=InferenceDescription.FAILURE,
        )
        failure_node = is_inference_included(infer_results, failure_inf)

        if worker_context.remaining_failovers > 0 and not failure_node:
            logger.info(
                f"[{worker_context.worker_spec.role}] Worker group "
                f"{worker_context.run_result.state.name}, "
                f"is failure node: {failure_node},"
                f"{worker_context.remaining_failovers}/"
                f"{worker_context.worker_spec.max_restarts} "
                f"attempts left; will restart worker group."
            )
            return DiagnosisAction.RESTART_WORKER
        else:
            logger.info(
                f"[{worker_context.worker_spec.role}] Worker group "
                f"{worker_context.run_result.state.name}, "
                f"is failure node: {failure_node}, "
                f"no attempts({worker_context.worker_spec.max_restarts}) "
                "left; will relaunch."
            )
            return DiagnosisAction.RELAUNCH_WORKER

    def _report_failure_to_master(
        self, failures: Dict[int, ProcessFailure], restart_count: int
    ):
        errors = {}
        if len(failures) == 0:
            return
        for rank, failure in failures.items():
            dt = str(datetime.fromtimestamp(int(failure.timestamp)))
            error = ProcessError(
                failure.local_rank, failure.exitcode, failure.message, dt
            )
            errors[rank] = error.__dict__
        error_data = json.dumps(errors)
        self._client.report_failures(
            error_data,
            restart_count,
            TrainingExceptionLevel.PROCESS_ERROR,
        )

    def _report_metric_to_master(self, agent_metric: WorkerTrainingMetric):
        self._client.report_diagnosis_agent_metrics(agent_metric)
