import collections
import logging
import json
import time
from pykube import Pod
from pathlib import Path

from .histogram import DecayingExponentialHistogram

CHECKPOINT_FILE_NAME = "checkpoint.json"

CPU_PERCENTILE = 0.9
CPU_SAFETY_MARGIN_FACTOR = 1.15
MEMORY_PERCENTILE = 1.0
MEMORY_SAFETY_MARGIN_FACTOR = 1.15
MIN_SAMPLE_WEIGHT = 0.1

ONE_DAY = 3600 * 24
CPU_HISTOGRAM_DECAY_HALF_LIFE = ONE_DAY
MEMORY_HISTOGRAM_DECAY_HALF_LIFE = ONE_DAY

logger = logging.getLogger(__name__)

def new_cpu_histogram():
    # CPU histograms use exponential bucketing scheme with the smallest bucket
    # size of 0.01 core, max of 1000.0 cores and the relative error of HistogramRelativeError.
    return DecayingExponentialHistogram(1000.0, 0.01, 1.05, CPU_HISTOGRAM_DECAY_HALF_LIFE)


def new_memory_histogram():
    # Memory histograms use exponential bucketing scheme with the smallest
    # bucket size of 10MB, max of 1TB and the relative error of HistogramRelativeError.
    return DecayingExponentialHistogram(1e12, 1e7, 1.05, MEMORY_HISTOGRAM_DECAY_HALF_LIFE)


class Recommender:
    def __init__(self):
        self.cpu_histograms = collections.defaultdict(new_cpu_histogram)
        self.memory_histograms = collections.defaultdict(new_memory_histogram)

    def update_pods(self, pods: dict):
        pods_by_aggregation_key = collections.defaultdict(list)
        now = time.time()

        for namespace_name, pod in pods.items():
            namespace, name = namespace_name
            aggregation_key = (namespace, pod["application"], pod["component"])
            pods_by_aggregation_key[aggregation_key].append(pod)

            cpu_histogram = self.cpu_histograms[aggregation_key]
            cpu_histogram.add_sample(pod["usage"]["cpu"], max(pod["requests"]["cpu"], MIN_SAMPLE_WEIGHT), now)

            memory_histogram = self.memory_histograms[aggregation_key]
            memory_histogram.add_sample(
                pod["usage"]["memory"], 1.0, now
            )

        for aggregation_key, pods_ in pods_by_aggregation_key.items():
            cpu_histogram = self.cpu_histograms[aggregation_key]
            cpu_recommendation = cpu_histogram.get_percentile(CPU_PERCENTILE) * CPU_SAFETY_MARGIN_FACTOR

            memory_histogram = self.memory_histograms[aggregation_key]
            memory_recommendation = memory_histogram.get_percentile(MEMORY_PERCENTILE) * MEMORY_SAFETY_MARGIN_FACTOR

            for pod in pods_:
                # don't overwrite any existing recommendations (e.g. from VPA)
                if "recommendation" not in pod:
                    pod["recommendation"] = {
                        "cpu": cpu_recommendation,
                        "memory": memory_recommendation,
                    }

    def load_from_file(self, data_path: Path):
        for path in data_path.rglob(CHECKPOINT_FILE_NAME):
            aggregation_key = tuple(path.parent.parts[-3:])
            try:
                with path.open() as fd:
                    data = json.load(fd)
                self.cpu_histograms[aggregation_key].from_checkpoint(data["cpu_histogram"])
                self.memory_histograms[aggregation_key].from_checkpoint(data["memory_histogram"])
            except Exception as e:
                logger.warning(f"Failed to load recommender checkpoint from {path}: {e}")


    def save_to_file(self, data_path: Path):
        for aggregation_key, cpu_histogram in self.cpu_histograms.items():
            folder = data_path
            for part in aggregation_key:
                folder /= part
            folder.mkdir(parents=True, exist_ok=True)
            data = {"cpu_histogram": cpu_histogram.get_checkpoint(),
                    "memory_histogram": self.memory_histograms[aggregation_key].get_checkpoint()}
            path = folder / CHECKPOINT_FILE_NAME
            with path.open("w") as fd:
                json.dump(data, fd)
