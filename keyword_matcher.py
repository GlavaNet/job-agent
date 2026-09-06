#!/usr/bin/env python3
"""
Match keywords from job descriptions against your resume.

Identifies which skills/keywords from a job posting you actually have,
so the tailor prompt can strategically highlight them.

This runs automatically as part of resume tailoring, but can also be
used standalone for analysis.

Usage (standalone):
  python3 keyword_matcher.py --job-id 123 --resume-text "your resume here"
"""
import re
from typing import Set, List, Tuple


# Keywords to extract from job descriptions
# Organized by category for better matching
JOB_KEYWORD_CATEGORIES = {
    "technologies": [
        # Cloud platforms
        "aws", "azure", "gcp", "google cloud",
        # Networking
        "cisco", "arista", "juniper", "bgp", "ospf", "mpls", "vlan", "firewall",
        "palo alto", "checkpoint", "fortinet", "junos", "iosxe", "ios-xe",
        # Systems
        "linux", "windows", "ubuntu", "rhel", "centos", "debian",
        "active directory", "entra id", "intune", "group policy",
        # Virtualization
        "vmware", "esxi", "hyper-v", "kvm", "docker", "kubernetes", "k8s",
        # Monitoring/Logging
        "splunk", "elk", "prometheus", "grafana", "datadog", "new relic",
        "siem", "syslog", "netflow",
        # Scripting/Automation
        "python", "powershell", "bash", "shell", "terraform", "ansible",
        "chef", "puppet", "jenkins", "ci/cd", "devops",
        # Databases
        "sql", "mysql", "postgresql", "oracle", "mongodb", "redis",
        # Security
        "ssl", "tls", "vpn", "ipsec", "mfa", "2fa", "duo", "okta",
        "zero trust", "incident response", "vulnerability management",
        # Collaboration
        "microsoft 365", "office 365", "exchange", "teams", "sharepoint",
        "slack", "confluence", "jira",
        # Other
        "api", "rest", "json", "xml", "ldap", "dns", "dhcp", "ntp",
        "snmp", "nms", "git", "github", "gitlab",
    ],
    "soft_skills": [
        "troubleshooting", "problem solving", "communication",
        "documentation", "technical writing", "leadership",
        "team player", "cross-functional", "collaboration",
        "customer service", "stakeholder management",
    ],
    "certifications": [
        "ccna", "ccnp", "ccie", "cissp", "sec+", "security+",
        "aws certified", "azure certified", "gcp certified",
        "linux+", "network+", "a+", "comptia",
    ],
}

# Compile all keywords into one searchable set, with normalized forms
ALL_KEYWORDS = set()
for category, keywords in JOB_KEYWORD_CATEGORIES.items():
    ALL_KEYWORDS.update(keywords)


def extract_keywords_from_text(text: str, min_length: int = 3) -> Set[str]:
    """
    Extract potential keywords from text.
    
    Returns normalized keywords (lowercase, no extra spaces).
    """
    # Split on whitespace and punctuation, keep hyphenated words
    words = re.findall(r"[\w\-]+", text.lower())
    
    # Filter to reasonable keyword length and known keywords
    keywords = set()
    for word in words:
        if len(word) >= min_length:
            # Check exact match
            if word in ALL_KEYWORDS:
                keywords.add(word)
            # Check for multi-word phrases by looking at the original text
    
    # Also look for known multi-word keywords
    text_lower = text.lower()
    for keyword in ALL_KEYWORDS:
        if " " in keyword and keyword in text_lower:
            keywords.add(keyword)
    
    return keywords


def match_keywords(job_description: str, resume_text: str) -> Tuple[List[str], List[str]]:
    """
    Match keywords between job description and resume.
    
    Returns:
        (matched_keywords, unmatched_keywords_from_job)
    
    matched_keywords: Keywords from job that appear in your resume (these you should highlight)
    unmatched_keywords: Keywords from job not in your resume (gaps you can't fill)
    """
    # Extract keywords from both texts
    job_keywords = extract_keywords_from_text(job_description)
    resume_keywords = extract_keywords_from_text(resume_text)
    
    # Find intersection (keywords you have)
    matched = sorted(list(job_keywords & resume_keywords))
    
    # Find keywords in job but not in resume (gaps)
    unmatched = sorted(list(job_keywords - resume_keywords))
    
    return matched, unmatched


def generate_keyword_hint(matched_keywords: List[str], max_keywords: int = 8) -> str:
    """
    Generate a hint string for the LLM about which keywords to prioritize.
    
    Limits to top N keywords to keep prompt concise.
    """
    if not matched_keywords:
        return ""
    
    # Limit to avoid bloating the prompt
    top_keywords = matched_keywords[:max_keywords]
    
    hint = f"""KEYWORD GUIDANCE:
The following keywords from the job appear in your background. Prioritize these:
{', '.join(top_keywords)}

When reordering bullets within each section, surface these skills prominently.
For example, if you have experience with {top_keywords[0]} and {top_keywords[1] if len(top_keywords) > 1 else 'other required skills'},
move those bullets higher in the list."""
    
    return hint


# Standalone test
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Match job keywords against resume")
    parser.add_argument("--job-id", type=int, help="Job ID to check")
    parser.add_argument("--job-description", help="Job description text")
    parser.add_argument("--resume-text", help="Resume text")
    
    args = parser.parse_args()
    
    if args.job_id:
        try:
            from database import get_job_by_id
            job = get_job_by_id(args.job_id)
            if not job:
                print(f"Job {args.job_id} not found")
                exit(1)
            
            job_desc = job.get("description", "")
            # Need resume text - would need to load from config
            print(f"Job {args.job_id}: {job.get('title')} @ {job.get('company')}")
            print(f"Description length: {len(job_desc)} chars")
            
            job_keywords = extract_keywords_from_text(job_desc)
            print(f"Keywords found: {sorted(list(job_keywords))[:15]}...")  # Show first 15
        except Exception as e:
            print(f"Error: {e}")
    
    elif args.job_description and args.resume_text:
        matched, unmatched = match_keywords(args.job_description, args.resume_text)
        print(f"Matched keywords ({len(matched)}): {matched}")
        print(f"\nUnmatched keywords ({len(unmatched)}): {unmatched}")
        print(f"\nHint for LLM:\n{generate_keyword_hint(matched)}")
