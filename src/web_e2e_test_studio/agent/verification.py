"""Deterministic post-action verification used before an asset commit."""

from __future__ import annotations

from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    JsonValue,
    Observation,
    VerificationResult,
    VerificationStatus,
)


class DeterministicVerifier:
    """Conservative verification using execution facts and a fresh snapshot."""

    def verify(
        self,
        action: Action,
        result: ActionResult,
        before: Observation,
        after: Observation,
        expected_result: str,
    ) -> VerificationResult:
        if not result.success:
            return VerificationResult(
                VerificationStatus.REJECTED,
                result.error or f"{action.name} failed",
                evidence={
                    "before_url": before.url,
                    "after_url": after.url,
                    "execution_status": result.status.value,
                },
            )

        evidence: dict[str, JsonValue] = {
            "before_url": before.url,
            "after_url": after.url,
            "expected_result": expected_result,
            "output": result.output,
        }
        if action.name == "extract_spreadsheet":
            matches = result.output.get("matches")
            if not isinstance(matches, list) or not matches:
                return VerificationResult(
                    VerificationStatus.REJECTED,
                    "The spreadsheet query returned no matches",
                    evidence=evidence,
                )
            return VerificationResult(
                VerificationStatus.VERIFIED,
                "The spreadsheet query returned matching values",
                evidence=evidence,
            )
        if action.name == "launch_application":
            running = result.output.get("running")
            pid = result.output.get("pid")
            if running is not True or not isinstance(pid, int):
                return VerificationResult(
                    VerificationStatus.REJECTED,
                    "The application did not pass the startup process check",
                    evidence=evidence,
                )
            return VerificationResult(
                VerificationStatus.VERIFIED,
                "The application process remained running during startup check",
                evidence=evidence,
            )
        if action.channel is ActionChannel.EXTERNAL:
            if "stdout" in result.output or "stderr" in result.output:
                stdout = str(result.output.get("stdout", "")).strip()
                stderr = str(result.output.get("stderr", "")).strip()
                if not stdout and not stderr:
                    return VerificationResult(
                        VerificationStatus.REJECTED,
                        "The external tool returned no output",
                        evidence=evidence,
                    )
            return VerificationResult(
                VerificationStatus.VERIFIED,
                "The external tool completed successfully",
                evidence=evidence,
            )
        if action.channel is ActionChannel.API:
            return VerificationResult(
                VerificationStatus.VERIFIED,
                "The API request returned the expected status",
                evidence=evidence,
            )

        if action.name == "navigate":
            expected_url = str(action.arguments.get("url", ""))
            if expected_url and _url_matches(after.url, expected_url):
                return VerificationResult(
                    VerificationStatus.VERIFIED,
                    "The browser reached the requested URL",
                    evidence=evidence,
                )
            return VerificationResult(
                VerificationStatus.REJECTED,
                f"Browser URL {after.url!r} does not match {expected_url!r}",
                evidence=evidence,
            )

        if not after.url and not after.title and not after.interactive_elements:
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                "The action succeeded but no fresh browser observation was available",
                evidence=evidence,
            )

        return VerificationResult(
            VerificationStatus.VERIFIED,
            "The deterministic browser action succeeded and the page was re-observed",
            evidence=evidence,
        )


def _url_matches(actual: str, expected: str) -> bool:
    return actual == expected or actual.rstrip("/") == expected.rstrip("/")


__all__ = ["DeterministicVerifier"]
