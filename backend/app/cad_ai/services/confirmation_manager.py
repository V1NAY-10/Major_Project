"""
Confidence Gating & User Confirmation
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ConfirmationRequest:
    """Request for user confirmation."""
    intent_id: str
    original_prompt: str
    interpretation: str
    confidence: float
    alternatives: List[str]
    risky: bool
    risk_description: Optional[str]


class ConfirmationManager:
    """Manages confidence gating."""
    
    CONFIDENCE_THRESHOLD = 0.75
    
    def needs_confirmation(self, disambiguated_intent) -> bool:
        """Check if confirmation is needed."""
        return disambiguated_intent.confidence_overall < self.CONFIDENCE_THRESHOLD
    
    def build_confirmation_request(self, disambiguated_intent) -> ConfirmationRequest:
        """Build confirmation request for user."""
        
        risky = self._assess_risk(disambiguated_intent)
        risk_desc = self._describe_risk(disambiguated_intent) if risky else None
        
        return ConfirmationRequest(
            intent_id=f"intent_{id(disambiguated_intent)}",
            original_prompt=disambiguated_intent.original_prompt,
            interpretation=self._build_interpretation_string(disambiguated_intent),
            confidence=disambiguated_intent.confidence_overall,
            alternatives=disambiguated_intent.alternative_interpretations,
            risky=risky,
            risk_description=risk_desc
        )
    
    def _assess_risk(self, intent) -> bool:
        """Determine if modification is risky."""
        
        if intent.modification_type == "scale":
            if intent.quantitative_value and intent.quantitative_value < 0.5:
                return True
            if intent.quantitative_value and intent.quantitative_value > 2.0:
                return True
        
        return False
    
    def _describe_risk(self, intent) -> str:
        """Describe the risk."""
        if intent.modification_type == "scale":
            scale_pct = int((intent.quantitative_value - 1) * 100) if intent.quantitative_value else 0
            return f"Scales dimensions by {scale_pct:+d}%, may affect structural integrity."
        
        return "This may have unintended consequences."
    
    def _build_interpretation_string(self, intent) -> str:
        """Build human-readable summary."""
        
        parts = []
        
        if intent.qualitative_direction:
            parts.append(intent.qualitative_direction)
        
        parts.append(intent.target_property)
        
        if intent.quantitative_value:
            if intent.modification_type == "scale":
                pct = int((intent.quantitative_value - 1) * 100)
                parts.append(f"by {pct:+d}%")
            else:
                parts.append(f"by {intent.quantitative_value}")
        
        num_targets = len(intent.target_clusters)
        parts.append(f"of {num_targets} component(s)")
        
        return " ".join(parts)
