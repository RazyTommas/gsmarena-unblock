"""Source-isolated ingestion for Mobile Observatory.

Collectors emit observations; they never write canonical entities.
"""

from .contracts import Observation, RawArtifact, SourceRun
from .importer import IngestionImporter
from .pipeline import CollectorPipeline, PipelineResult
from .promotion import PromotionResult, SamsungFirmwarePromoter

__all__ = ["CollectorPipeline", "IngestionImporter", "Observation", "PipelineResult", "PromotionResult", "RawArtifact", "SamsungFirmwarePromoter", "SourceRun"]
