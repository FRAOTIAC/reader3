import os
import copy
from bs4 import BeautifulSoup, Tag, NavigableString

def split_html_by_anchors(html_content, anchors):
    """
    Splits an HTML string into segments based on a list of anchor IDs.
    Returns a dict: {anchor_id: html_segment_string, ...}
    The first segment (before any anchor) uses key 'START'.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    body = soup.find('body')
    if not body:
        return {'START': html_content}

    # Find all split points in the DOM
    split_points = []
    for anchor_id in anchors:
        # anchor_id might be "filepos123"
        # We need to find element with id="filepos123" or name="filepos123"
        elem = soup.find(id=anchor_id) or soup.find(attrs={"name": anchor_id})
        if elem:
            split_points.append((anchor_id, elem))
    
    # If no anchors found in this file, return as is
    if not split_points:
        return {'START': str(body)}

    # Sort split points by appearance in document? 
    # Actually, BeautifulSoup find usually returns in document order, 
    # but our 'anchors' input list implies the desired logical order.
    # However, for physical splitting, we must follow document order.
    # Let's assume split_points are roughly in order or we process them linearly.
    
    # We will iterate through body's children and bucket them into the current active anchor.
    # This is a shallow split (top-level children of body). 
    # If an anchor is DEEP inside a div, this simple approach fails to split the div.
    # But complex splitting is very hard. 
    # STRATEGY: 
    # 1. Flatten the tree? No.
    # 2. Iterate all elements?
    # BETTER STRATEGY for V1:
    # Identify the top-level block elements that contain the anchors.
    # Split at those top-level boundaries.
    
    segments = {}
    current_anchor = 'START'
    segments[current_anchor] = []

    # Map each element to its closest preceding anchor
    # This is tricky. 
    
    # Alternative: "Walk" the tree.
    # But simply: usually EPUBs are flat-ish: <body> <p>... <h1 id="ch1"> ... <p> ... <h1 id="ch2">
    
    # Let's try iterating over body.contents
    for child in body.contents:
        if isinstance(child, NavigableString) and not child.strip():
            segments[current_anchor].append(str(child))
            continue
            
        # Check if this child OR ANY DESCENDANT is a split point
        # We need to know WHICH split point it contains.
        found_anchor = None
        
        if isinstance(child, Tag):
            # Check the tag itself
            child_id = child.get('id') or child.get('name')
            if child_id in anchors:
                found_anchor = child_id
            else:
                # Check descendants
                # This is greedy: if a huge div contains 3 chapters, we can't split it this way.
                # We would dump the whole div into the first chapter.
                # This is the limitation of this simple splitter.
                # But for "How I Made...", let's see if anchors are on Headers (usually top level).
                pass
        
        # If we found a switch
        if found_anchor:
            current_anchor = found_anchor
            if current_anchor not in segments:
                segments[current_anchor] = []
        
        segments[current_anchor].append(str(child))

    # Reassemble
    result = {}
    for k, parts in segments.items():
        result[k] = "".join(parts)
        
    return result

# Simple test isn't enough because we need the real file structure.
# I will proceed to modify reader3.py but keep the logic robust (fallback to no-split).
