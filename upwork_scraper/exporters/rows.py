"""Convert processed leads to the stable export row schema."""

from datetime import datetime, timezone, tzinfo
from email.utils import parsedate_to_datetime

from ..pipeline.processor import ProcessedLead


def _parse_utc_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def processed_lead_to_row(
    item: ProcessedLead,
    sheet_saved_at: datetime | None = None,
    display_timezone: tzinfo | None = None,
) -> dict[str, str]:
    lead = item.lead
    analysis = item.analysis
    found_at = _parse_utc_timestamp(lead.scraped_at)
    saved_at = sheet_saved_at
    if saved_at is not None:
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        else:
            saved_at = saved_at.astimezone(timezone.utc)

    duration = ""
    if found_at is not None and saved_at is not None:
        duration = f"{max(0.0, (saved_at - found_at).total_seconds()):.2f}"

    posted_display = lead.posted_date
    if lead.platform == "Upwork (Vollna)":
        try:
            posted_at = parsedate_to_datetime(lead.posted_date)
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            reference = saved_at or datetime.now(timezone.utc)
            age_seconds = max(0, int((reference - posted_at).total_seconds()))
            amount, unit = ((age_seconds, "second") if age_seconds < 60
                            else (age_seconds // 60, "minute"))
            posted_display = f"{amount} {unit}{'' if amount == 1 else 's'} ago"
        except (TypeError, ValueError, OverflowError):
            pass

    def display_timestamp(value: datetime | None, fallback: str = "") -> str:
        if value is None:
            return fallback
        if display_timezone is None:
            return value.isoformat(timespec="seconds")
        local_value = value.astimezone(display_timezone)
        local_time = local_value.strftime("%I:%M:%S %p").lstrip("0")
        return f"{local_value:%Y-%m-%d} {local_time}"

    return {
        "Job Title": lead.title,
        "Job URL": lead.url,
        "Job Platform": lead.platform,
        "Date Posted": posted_display,
        "Lead Found At": display_timestamp(found_at, lead.scraped_at),
        "Sheet Saved At": display_timestamp(saved_at),
        "Found-to-Sheet Seconds": duration,
        "Attachment Status": lead.attachment_status,
        "Attachment Count": str(lead.attachment_count) if lead.attachment_count is not None else "",
        "Priority": analysis.priority,
        "Lead Score": str(analysis.lead_score),
        "Company Name": analysis.company_name,
        "Company Website": analysis.company_website,
        "Company Domain": analysis.company_domain,
        "Email": analysis.email,
        "Business Email": analysis.business_email,
        "Phone": analysis.phone,
        "LinkedIn URL": analysis.linkedin_url,
        "Decision-Maker Name": analysis.decision_maker_name,
        "Decision-Maker Title": analysis.decision_maker_title,
        "Budget": analysis.budget if analysis._found_budget else lead.budget,
        "Timeline": analysis.timeline,
        "Location": lead.location or lead.country or analysis.location,
        "Industry": analysis.industry,
        "Technologies": analysis.technologies or lead.skills_required,
        "Services Required": analysis.services_required,
        "Qualification Reason": analysis.qualification_reason,
        "Full Job Description": (lead.description or "")[:1000],
        "Attachment Names": "\n".join(lead.attachment_names),
        "Attachment URLs": "\n".join(lead.attachment_urls),
    }
