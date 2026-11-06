"""Publication must describe a real item during the exact legal entity's existence."""
from __future__ import annotations
import re
from datetime import date
from urllib.parse import urlsplit


def publication_eligibility(profile, published, url='', text=''):
    try: publication=date.fromisoformat(str(published)[:10])
    except ValueError:return False,'missing_supported_publication_date'
    registry=(profile.get('evidence',{}).get('registry',{}).get('value') or {})
    live=(profile.get('evidence',{}).get('registry_live',{}).get('value') or {})
    founded=registry.get('stiftelsesdato') or live.get('established_date') or live.get('stiftelsesdato')
    if founded:
        try:
            if publication < date.fromisoformat(str(founded)[:10]):return False,'predates_exact_legal_entity'
        except ValueError:pass
    placeholder=re.search(r'welcome to wordpress|this is your first post|edit or delete it|velkommen til wordpress|dette er ditt f[oø]rste innlegg|dette er den f[oø]rste kommentaren|commenter avatars come from gravatar|moderate, edit, and delete comments',text,re.I)
    default_slug=urlsplit(url).path.rstrip('/').endswith(('/hello-world','/hei-verden'))
    if placeholder or default_slug and re.search(r'hello world|hei verden|wordpress|comment',text,re.I):return False,'default_template_content'
    return True,None
