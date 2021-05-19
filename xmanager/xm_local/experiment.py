# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of the local scheduler experiment."""

import asyncio
from concurrent import futures
import time
from typing import Any, Awaitable, Callable, Iterable, List, Mapping

from xmanager import xm
from xmanager.cloud import caip
from xmanager.cloud import kubernetes
from xmanager.xm import id_predictor
from xmanager.xm import pattern_matching
from xmanager.xm import utils
from xmanager.xm_local import execution as local_execution
from xmanager.xm_local import executors as local_executors
from xmanager.xm_local.packaging import router as packaging_router


def _throw_on_unknown_executor(job: xm.Job, executor: Any):
  raise TypeError(f'Unsupported executor: {executor!r}. Job: {job!r}')


_EXECUTOR_VALIDATOR = pattern_matching.match(
    pattern_matching.Case([xm.Job, local_executors.Local], lambda *_: None),
    pattern_matching.Case([xm.Job, local_executors.Caip], lambda *_: None),
    pattern_matching.Case([xm.Job, local_executors.Kubernetes],
                          lambda *_: None),
    _throw_on_unknown_executor,
)


def _validate_job_group(job_group: xm.JobGroup) -> None:
  all_jobs = utils.collect_jobs_by_filter(job_group, lambda _: True)
  for job in all_jobs:
    _EXECUTOR_VALIDATOR(job, job.executor)


class LocalWorkUnit(xm.WorkUnit):
  """WorkUnit operated by the local backend."""

  def __init__(self, experiment: 'LocalExperiment', experiment_name: str,
               work_unit_id_predictor: id_predictor.Predictor,
               create_task: Callable[[Awaitable[Any]], futures.Future]) -> None:
    super().__init__(experiment, work_unit_id_predictor, create_task)
    self._experiment_name = experiment_name
    self._local_execution_handles: List[
        local_execution.LocalExecutionHandle] = []
    self._non_local_execution_handles: List[
        local_execution.ExecutionHandle] = []

  async def _launch_job_group(self, job_group: xm.JobGroup,
                              args: Mapping[str, Any]) -> None:
    del args  # Unused.
    _validate_job_group(job_group)
    # We are delegating the traversal of the job group to modules. That improves
    # modularity, but sacrifices the ability to make cross-executor decisions.
    async with self._work_unit_id_predictor.submit_id(self.work_unit_id):
      self._non_local_execution_handles.extend(
          caip.launch(self._experiment_name, self.experiment_id,
                      self.work_unit_id, job_group))
      self._non_local_execution_handles.extend(
          kubernetes.launch(self._experiment_name, self.experiment_id,
                            self.work_unit_id, job_group))
      self._local_execution_handles.extend(await
                                           local_execution.launch(job_group))

  async def _wait_until_complete(self) -> None:
    try:
      await asyncio.gather(*[
          handle.wait() for handle in self._local_execution_handles +
          self._non_local_execution_handles
      ])
    except RuntimeError as error:
      raise xm.WorkUnitFailedError(error)

  async def wait_for_local_jobs(self, is_exit_abrupt: bool):
    if not is_exit_abrupt:
      await asyncio.gather(
          *[handle.wait() for handle in self._local_execution_handles])


class LocalExperiment(xm.Experiment):
  """Experiment contains a family of jobs that run with the local scheduler."""

  _id: int
  _experiment_name: str
  _work_units: List[LocalWorkUnit]

  def __init__(self, experiment_name: str) -> None:
    super().__init__()
    # To distinguish local job names until we have a local database.
    self._id = int(time.time() * 10**3)
    self._experiment_name = experiment_name
    self._work_units = []

  def package(
      self, packageables: Iterable[xm.Packageable]) -> Iterable[xm.Executable]:
    """Packages executable specs into executables based on the executor specs."""
    return [
        packaging_router.package(packageable) for packageable in packageables
    ]

  def _create_work_unit(self) -> LocalWorkUnit:
    work_unit = LocalWorkUnit(self, self._experiment_name,
                              self._work_unit_id_predictor, self._create_task)
    self._work_units.append(work_unit)
    return work_unit

  def _wait_for_local_jobs(self, is_exit_abrupt: bool):
    if self._work_units:
      print('Waiting for local jobs to complete.'
            ' Press Ctrl+C to terminate them and exit')
    for work_unit in self._work_units:
      self._create_task(work_unit.wait_for_local_jobs(is_exit_abrupt))

  def __exit__(self, exc_type, exc_value, traceback):
    self._wait_for_local_jobs(exc_value is not None)
    return super().__exit__(exc_type, exc_value, traceback)

  async def __aexit__(self, exc_type, exc_value, traceback):
    self._wait_for_local_jobs(exc_value is not None)
    return await super().__aexit__(exc_type, exc_value, traceback)

  @property
  def experiment_id(self) -> int:
    return self._id

  @property
  def work_unit_count(self) -> int:
    return len(self._work_units)


def create_experiment(experiment_name: str) -> xm.Experiment:
  """Create Experiment."""
  return LocalExperiment(experiment_name)
