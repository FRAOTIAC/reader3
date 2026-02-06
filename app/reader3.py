"""
Parses an EPUB file into a structured object that can be used to serve the book via a web interface.
"""

import os
import pickle
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment, Tag, NavigableString

# --- Data structures ---

@dataclass
class ChapterContent:
    """
    Represents a logical chapter unit to be displayed.
    Usually corresponds to a spine item, but can be a fragment of one if split by TOC anchors.
    """
    id: str           # Internal ID (e.g., 'item_1' or 'item_1_part2')
    href: str         # Filename (e.g., 'part01.html')
    title: str        # Best guess title
    content: str      # Cleaned HTML
    text: str         # Plain text
    order: int        # Linear reading order


@dataclass
class TOCEntry:
    """Represents a logical entry in the navigation sidebar."""
    title: str
    href: str         # original href (e.g., 'part01.html#chapter1')
    file_href: str    # just the filename (e.g., 'part01.html')
    anchor: str       # just the anchor (e.g., 'chapter1'), empty if none
    children: List['TOCEntry'] = field(default_factory=list)


@dataclass
class BookMetadata:
    """Metadata"""
    title: str
    language: str
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    publisher: Optional[str] = None
    date: Optional[str] = None
    identifiers: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass
class Book:
    """The Master Object to be pickled."""
    metadata: BookMetadata
    spine: List[ChapterContent]  # The logical reading order (can be split files)
    toc: List[TOCEntry]          # The navigation tree
    images: Dict[str, str]       # Map: original_path -> local_path
    
    # Meta info
    source_file: str
    processed_at: str
    cover_image: Optional[str] = None # Path to cover image relative to book root
    version: str = "3.2" # Bumped version for cover support


# --- Utilities ---

def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:

    # Remove dangerous/useless tags
    for tag in soup(['script', 'style', 'iframe', 'video', 'nav', 'form', 'button']):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove input tags
    for tag in soup.find_all('input'):
        tag.decompose()

    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    """Extract clean text for LLM/Search usage."""
    text = soup.get_text(separator=' ')
    # Collapse whitespace
    return ' '.join(text.split())


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    """
    Recursively parses the TOC structure from ebooklib.
    """
    result = []

    for item in toc_list:
        # ebooklib TOC items are either `Link` objects or tuples (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            entry = TOCEntry(
                title=section.title,
                href=section.href,
                file_href=section.href.split('#')[0],
                anchor=section.href.split('#')[1] if '#' in section.href else "",
                children=parse_toc_recursive(children, depth + 1)
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
            result.append(entry)
        # Note: ebooklib sometimes returns direct Section objects without children
        elif isinstance(item, epub.Section):
             entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
             result.append(entry)
    
    # Clean titles (limit length)
    for entry in result:
        if len(entry.title) > 80:
            entry.title = entry.title[:80] + "..."

    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    """
    If TOC is missing, build a flat one from the Spine.
    """
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            # Try to guess a title from the content or ID
            title = item.get_name().replace('.html', '').replace('.xhtml', '').replace('_', ' ').title()
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata_robust(book_obj) -> BookMetadata:
    """
    Extracts metadata handling both single and list values.
    """
    def get_list(key):
        data = book_obj.get_metadata('DC', key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata('DC', key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one('title') or "Untitled",
        language=get_one('language') or "en",
        authors=get_list('creator'),
        description=get_one('description'),
        publisher=get_one('publisher'),
        date=get_one('date'),
        identifiers=get_list('identifier'),
        subjects=get_list('subject')
    )

def flatten_toc(toc_entries: List[TOCEntry]) -> List[TOCEntry]:
    """Returns a flat list of all TOC entries for easier lookup."""
    flat = []
    for entry in toc_entries:
        flat.append(entry)
        if entry.children:
            flat.extend(flatten_toc(entry.children))
    return flat

def split_html_by_anchors(soup: BeautifulSoup, anchors: List[str]) -> Dict[str, str]:
    """
    Splits the DOM tree in `soup` based on a list of anchor IDs.
    Strategy: 
      1. Find all anchor elements.
      2. Identify their top-level parent (direct child of body).
      3. Iterate through body's children, bucketizing them into segments based on the most recently seen anchor parent.
    
    Returns: {anchor_id: html_string}
    The segment before the first anchor is keyed as 'START'.
    """
    body = soup.find('body')
    if not body:
        return {'START': str(soup)}

    # Map anchor_id -> Element
    anchor_map = {}
    for aid in anchors:
        # IDs are unique
        elem = soup.find(id=aid) or soup.find(attrs={"name": aid})
        if elem:
            anchor_map[aid] = elem
    
    # If no anchors found in DOM, return whole
    if not anchor_map:
        return {'START': "".join([str(c) for c in body.contents])}

    # Identify "Split Points": The direct children of body that contain the anchors
    # We walk up from the anchor element until we hit body.
    split_points = {} # Element -> anchor_id
    
    for aid, elem in anchor_map.items():
        parent = elem
        # Walk up
        while parent.parent and parent.parent.name != 'body' and parent.parent.name != '[document]':
            parent = parent.parent
        
        # parent is now a direct child of body (or body itself if something is weird)
        # We record that this element starts a new section for 'aid'
        # Note: if multiple anchors are in the same block, the last one wins? 
        # No, we want the first one to claim it? Or maybe we can't split inside a block.
        # We'll use the first one encountered.
        if parent not in split_points:
             split_points[parent] = aid

    # Now iterate body contents and bucket
    segments = {}
    current_key = 'START'
    segments[current_key] = []

    for child in body.contents:
        # Is this child a split point?
        if child in split_points:
            current_key = split_points[child]
            if current_key not in segments:
                segments[current_key] = []
        
        segments[current_key].append(str(child))

    # Join
    result = {}
    for k, v in segments.items():
        result[k] = "".join(v)
    
    return result

# --- Main Conversion Logic ---

def process_epub(epub_path: str, output_dir: str) -> Book:

    # 1. Load Book
    print(f"Loading {epub_path}...")
    book = epub.read_epub(epub_path)

    # 2. Extract Metadata
    metadata = extract_metadata_robust(book)

    # 3. Prepare Output Directories
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)

    # 4. Extract Images & Build Map
    print("Extracting images...")
    image_map = {} # Key: internal_path, Value: local_relative_path
    cover_image_path = None

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            # Normalize filename
            original_fname = os.path.basename(item.get_name())
            # Sanitize filename for OS
            safe_fname = "".join([c for c in original_fname if c.isalpha() or c.isdigit() or c in '._-']).strip()

            # Save to disk
            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, 'wb') as f:
                f.write(item.get_content())

            # Map keys: We try both the full internal path and just the basename
            # to be robust against messy HTML src attributes
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path
    
    # Identify Cover Image
    # 1. Check for 'cover-image' in manifest properties (Epub 3)
    # 2. Check metadata 'cover' which references an ID (Epub 2)
    # 3. Heuristic: Look for 'cover' in filename
    
    cover_id = None
    
    # Try getting cover from metadata (Epub 2)
    try:
        covers = book.get_metadata('OPF', 'cover')
        if covers:
            cover_id = covers[0][0]
    except:
        pass
        
    # Try manifest item properties (Epub 3)
    if not cover_id:
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_IMAGE:
                # Check for cover property in manifest
                # ebooklib stores manifest attributes in a private or less accessible way
                # but we can try to guess from the item id or name if metadata failed
                if 'cover' in item.get_id().lower():
                    cover_id = item.get_id()
                    break

    if cover_id:
        item = book.get_item_with_id(cover_id)
        if item:
            cover_image_path = image_map.get(item.get_name())
    
    # Fallback heuristics: search for 'cover' in filename if not found yet
    if not cover_image_path:
        # Sort by length to prefer shorter names like 'cover.jpg' over 'chapter1_cover.jpg'
        possible_covers = []
        for original_name, local_path in image_map.items():
            if 'cover' in original_name.lower() and original_name != '__COVER__':
                possible_covers.append(local_path)
        
        if possible_covers:
            # Pick the one that most likely is a cover (shortest name usually)
            cover_image_path = min(possible_covers, key=len)
                
    # Add cover to image_map with a standard key for easier access
    if cover_image_path:
        image_map['__COVER__'] = cover_image_path

    # 5. Process TOC
    print("Parsing Table of Contents...")
    toc_structure = parse_toc_recursive(book.toc)
    if not toc_structure:
        print("Warning: Empty TOC, building fallback from Spine...")
        toc_structure = get_fallback_toc(book)

    # Flatten TOC for easy lookup of anchors per file
    flat_toc = flatten_toc(toc_structure)
    # Map: filename -> list of (anchor, title)
    file_toc_map = {}
    for entry in flat_toc:
        if entry.file_href not in file_toc_map:
            file_toc_map[entry.file_href] = []
        if entry.anchor:
            file_toc_map[entry.file_href].append((entry.anchor, entry.title))
    
    # 6. Process Content (Logical Splitting)
    print("Processing chapters...")
    spine_chapters = []
    global_order = 0

    # We iterate over the spine (linear reading order)
    for spine_item in book.spine:
        item_id, linear = spine_item
        item = book.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            file_name = item.get_name()
            
            # Raw content
            raw_content = item.get_content().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(raw_content, 'html.parser')

            # A. Fix Images
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if not src: continue
                # Decode URL
                src_decoded = unquote(src)
                filename = os.path.basename(src_decoded)
                # Try to find in map
                if src_decoded in image_map:
                    img['src'] = image_map[src_decoded]
                elif filename in image_map:
                    img['src'] = image_map[filename]

            # B. Clean HTML
            soup = clean_html_content(soup)
            
            # C. Check if we need to split this file
            # Get expected anchors for this file from TOC
            toc_anchors = []
            toc_titles_map = {} # anchor -> title
            
            if file_name in file_toc_map:
                for anch, tit in file_toc_map[file_name]:
                    toc_anchors.append(anch)
                    toc_titles_map[anch] = tit
            
            # Perform split
            # If no anchors in TOC or only 1, maybe we don't strictly need to split?
            # But if the file is huge and has 1 anchor halfway through...
            # For safety, if there are anchors, we try to split.
            
            segments = split_html_by_anchors(soup, toc_anchors)
            
            # D. Create Chapter Objects from Segments
            # Segments dict keys are anchor_ids (or 'START')
            # We want to maintain the order they appear in the file.
            # `split_html_by_anchors` returns a dict, order is not guaranteed in Py < 3.7 (though we use 3.x).
            # But we generated it by iterating body.
            
            # Re-sort segments based on body order? 
            # Our split function returns them in order of encounter if we iterate body.
            # But the dict keys might be shuffled? No, Python 3.7+ preserves insertion order.
            
            # However, `split_html_by_anchors` as implemented above buckets by key. 
            # If the body has: Start -> A -> Start -> B -> A
            # Our simple bucket impl would merge the two 'Start' blocks.
            # That might be wrong if 'A' is supposed to be a chapter in the middle.
            # But standard EPUBs usually don't interleave chapters like that.
            
            for seg_key, seg_html in segments.items():
                if not seg_html.strip():
                    continue
                
                # Determine title
                seg_title = f"Section {global_order+1}"
                if seg_key in toc_titles_map:
                    seg_title = toc_titles_map[seg_key]
                elif seg_key == 'START':
                    # Maybe this file *starts* with a chapter but has no anchor for it?
                    # Or it's the cover/title page.
                    pass

                # Create Object
                # Append segment key to ID to make it unique
                unique_id = f"{item_id}_{seg_key}" if seg_key != 'START' else item_id
                
                # Determine precise href
                # If this segment is from an anchor, append it. 
                # If it's START, use filename.
                if seg_key == 'START':
                    final_href = file_name
                else:
                    final_href = f"{file_name}#{seg_key}"

                # We need a new soup for text extraction to avoid processing the whole file again
                seg_soup = BeautifulSoup(seg_html, 'html.parser')

                chapter = ChapterContent(
                    id=unique_id,
                    href=final_href, # Important: precise href with anchor
                    title=seg_title,
                    content=seg_html,
                    text=extract_plain_text(seg_soup),
                    order=global_order
                )
                spine_chapters.append(chapter)
                global_order += 1

    # 7. Final Assembly
    final_book = Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        cover_image=cover_image_path,
        source_file=os.path.basename(epub_path),
        processed_at=datetime.now().isoformat()
    )

    return final_book


def save_to_pickle(book: Book, output_dir: str):
    p_path = os.path.join(output_dir, 'book.pkl')
    with open(p_path, 'wb') as f:
        pickle.dump(book, f)
    print(f"Saved structured data to {p_path}")


# --- CLI ---

if __name__ == "__main__":

    import sys
    if len(sys.argv) < 2:
        print("Usage: python reader3.py <file.epub>")
        sys.exit(1)

    epub_file = sys.argv[1]
    assert os.path.exists(epub_file), "File not found."
    out_dir = os.path.splitext(epub_file)[0] + "_data"

    book_obj = process_epub(epub_file, out_dir)
    save_to_pickle(book_obj, out_dir)
    print("\n--- Summary ---")
    print(f"Title: {book_obj.metadata.title}")
    print(f"Authors: {', '.join(book_obj.metadata.authors)}")
    print(f"Logical Chapters (Spine): {len(book_obj.spine)}")
    print(f"TOC Root Items: {len(book_obj.toc)}")
    print(f"Images extracted: {len(book_obj.images)}")
