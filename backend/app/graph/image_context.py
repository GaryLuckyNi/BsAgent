from app.graph.state import ImageAnalysisSummary


def summarize_image_analyses(
    analyses: list[ImageAnalysisSummary],
) -> list[dict]:
    return [
        {
            "analysis_id": analysis["analysis_id"],
            "document_type": analysis["document_type"],
            "summary": analysis["summary"],
            "entities": analysis["entities"],
            "warnings": analysis["risk_flags"],
            "confidence": analysis["confidence"],
            "source_trust": "untrusted_image_data",
        }
        for analysis in analyses
    ]
