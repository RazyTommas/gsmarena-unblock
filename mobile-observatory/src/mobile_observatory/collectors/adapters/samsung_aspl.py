from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..base import SourceAdapter, SourceHealthPolicy
from ..contracts import Observation, RawArtifact


class SamsungAsplAdapter(SourceAdapter):
    """Android security patch level per Samsung build, from Samsung's own publisher.

    WHY THIS SOURCE MATTERS MORE THAN ITS ROW COUNT SUGGESTS
    A patch level is the binding constraint on every security verdict. Without one,
    a device's exposure cannot be adjudicated at all -- not "assumed safe", not
    "assumed open", simply undecidable. This corpus reaches 0 device verdicts today
    and the legacy system reaches 307, and the difference is very largely this file.

    WHAT THE VALUE IS AND IS NOT
    `spl` is a full date such as 2026-08-05, and the DAY is load-bearing. Google
    publishes two patch tiers: -01 carries Framework, System and Play system updates,
    while -05 additionally carries Kernel, Arm, MediaTek and Qualcomm. So a build at
    -01 cannot adjudicate a chipset CVE however recent its month is. Storing the month
    alone would silently discard that, and every chipset verdict derived from it would
    be wrong in the permissive direction.

    Google never states the -01/-05 split in prose; it is structural in every bulletin
    since the split in 2016-07. Two rare cases the legacy system had to guard and this
    one inherits: a -06 tier exists and is ad hoc (2023-10 carried a late System patch
    under it), and before 2016-07 there was only ONE tier, so a -01 of that era was the
    whole bulletin including vendor fixes.

    PROVENANCE
    doc.samsungmobile.com is Samsung publishing about Samsung firmware, so the value
    and the device->value mapping are both the vendor's. That is the strongest
    authority tier this project recognises.

    A CHECK THAT ALREADY RAN, recorded because it is the reason to trust the column:
    at collection the day-of-month distribution was required to fall entirely on 01 or
    05, and a scattered distribution aborts the import rather than warning. That test
    has caught three different sources offering a RELEASE date under a patch-level
    name -- mifirm's upload timestamps, sammobile's CP prose, and HMD's
    dateOfFirstLiveRelease. It is cheap and it is not optional.
    """

    source_id = "samsung.doc.aspl"
    parser_name = "samsung_aspl_csv"
    parser_version = "1.0.0"
    health_policy = SourceHealthPolicy(
        minimum_observations=1000,
        required_kinds=("security_patch_publication",),
    )

    def __init__(self, artifact: Path, observed_at: str = "2026-09-11T14:32:19Z"):
        self.artifact = artifact
        self.observed_at = observed_at

    def fetch(self) -> Iterable[RawArtifact]:
        content = self.artifact.read_bytes()
        yield RawArtifact(self.source_id, self.observed_at, "text/csv", content,
                          self.artifact.resolve().as_uri(), 200)

    def parse(self, artifact: RawArtifact, artifact_sha256: str) -> Iterable[Observation]:
        text = artifact.content.decode("utf-8-sig")
        for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            spl = (row.get("spl") or "").strip()
            build = (row.get("build") or "").strip()
            model = (row.get("model") or "").strip()
            if not (spl and build and model):
                continue
            # The tier is derived here rather than stored upstream so the rule lives in
            # one place. None means "we cannot classify it", and a caller must treat
            # that as undecidable rather than comparing dates -- an unclassifiable tier
            # is a reason to say nothing, not a reason to guess.
            tier = None
            if len(spl) == 10:
                if spl < "2016-07-01":
                    tier = 5          # single-tier era: the one level was the whole bulletin
                elif spl.endswith("-05"):
                    tier = 5
                elif spl.endswith("-01"):
                    tier = 1
            yield Observation(
                "security_patch_publication",
                self.source_id,
                f"{model}:{row.get('csc', '')}:{build}",
                row.get("fetched_at") or self.observed_at,
                artifact_sha256,
                {
                    # the contract's required trio
                    "device": model,
                    "aspl_month": spl[:7],
                    "publish_date": (row.get("fetched_at") or self.observed_at)[:10],
                    # the exact day, supplied because Samsung published one -- never
                    # synthesised. aspl_month above stays month-precision so the
                    # "do not invent a day" rule is untouched.
                    "aspl_date": spl if len(spl) == 10 else None,
                    "patch_tier": tier,
                    "tier_basis": "bulletin-structure",
                    "model_code": model,
                    "region_code": (row.get("csc") or "").strip() or None,
                    "build": build,
                    "date_basis": "vendor_published_patch_level_not_release_date",
                },
                {"manufacturer": "Samsung", "model_code": model,
                 "region_code": (row.get("csc") or "").strip() or None},
                {"artifact_pointer": f"CSV line {line}",
                 "authority": "vendor-official",
                 "source_url": "https://doc.samsungmobile.com/"},
            )
