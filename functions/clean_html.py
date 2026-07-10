import re
from bs4 import BeautifulSoup, Comment

async def clean_html(html: str) -> str:
    """Minifies HTML, CSS, and JavaScript without script protection"""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove all comments
    for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Minify CSS in style tags and attributes
    for tag in soup.find_all(['style']):
        if tag.string:
            tag.string = re.sub(r'/\*.*?\*/', '', tag.string, flags=re.DOTALL)  # Remove CSS comments
            tag.string = re.sub(r'\s+', ' ', tag.string)  # Collapse whitespace
            tag.string = re.sub(r'\s*([{}:;,])\s*', r'\1', tag.string)  # Remove spaces around CSS delimiters
            tag.string = tag.string.strip()

    # Minify JavaScript in script tags
    for tag in soup.find_all('script'):
        if tag.string and not tag.get('src'):
            tag.string = re.sub(r'//.*?\n', '', tag.string)  # Remove JS comments
            tag.string = re.sub(r'/\*.*?\*/', '', tag.string, flags=re.DOTALL)
            tag.string = re.sub(r'\s+', ' ', tag.string)  # Collapse whitespace
            tag.string = tag.string.strip()

    # Minify HTML structure
    html_str = str(soup)
    html_str = re.sub(r'<!--.*?-->', '', html_str, flags=re.DOTALL)  # Remove HTML comments
    html_str = re.sub(r'>\s+<', '><', html_str)  # Remove whitespace between tags
    html_str = re.sub(r'\s{2,}', ' ', html_str)  # Collapse multiple spaces
    return html_str.strip()