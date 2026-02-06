import os
import shutil
import pickle
import json
from datetime import datetime
from functools import lru_cache
from typing import Optional, List, Tuple

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from reader3 import Book, BookMetadata, ChapterContent, TOCEntry, process_epub, save_to_pickle

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Where are the book folders located?
BOOKS_DIR = os.environ.get("BOOKS_DIR", ".")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json")

def get_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def update_history(book_id: str):
    history = get_history()
    history[book_id] = datetime.now().isoformat()
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(BOOKS_DIR, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None

@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request, sort: Optional[str] = None):
    """Lists all available processed books."""
    books = []
    history = get_history()
    
    # Determine sorting preference: Query param -> Cookie -> Default
    current_sort = sort or request.cookies.get("sort_pref") or "upload"

    # Scan directory for folders ending in '_data' that have a book.pkl
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                # Try to load it to get the title
                book = load_book_cached(item)
                if book:
                    # Resolve cover image
                    cover_url = None
                    if '__COVER__' in book.images:
                        cover_path = book.images['__COVER__']
                        cover_filename = os.path.basename(cover_path)
                        cover_url = f"/read/{item}/images/{cover_filename}"
                    else:
                        # Runtime fallback for books processed with older version
                        for img_internal_path, img_rel_path in book.images.items():
                            if 'cover' in img_internal_path.lower():
                                cover_filename = os.path.basename(img_rel_path)
                                cover_url = f"/read/{item}/images/{cover_filename}"
                                break
                        
                    books.append({
                        "id": item,
                        "title": book.metadata.title,
                        "author": ", ".join(book.metadata.authors),
                        "chapters": len(book.spine),
                        "cover_url": cover_url,
                        "processed_at": getattr(book, "processed_at", "1970-01-01T00:00:00"),
                        "last_opened": history.get(item, "1970-01-01T00:00:00")
                    })

    # Sorting
    if current_sort == "opened":
        books.sort(key=lambda x: x["last_opened"], reverse=True)
    else: # default to upload
        books.sort(key=lambda x: x["processed_at"], reverse=True)

    response = templates.TemplateResponse("library.html", {
        "request": request, 
        "books": books,
        "current_sort": current_sort
    })
    
    # Save preference in cookie for 30 days
    response.set_cookie(key="sort_pref", value=current_sort, max_age=30*24*60*60)
    return response

@app.post("/upload")
async def upload_books(files: List[UploadFile] = File(...)):
    """
    Uploads multiple EPUB files, converts them, and adds them to the library.
    """
    upload_dir = "uploads"
    if not os.path.exists(upload_dir):
        os.makedirs(upload_dir)

    for file in files:
        if not file.filename.lower().endswith(".epub"):
            print(f"Skipping non-epub file: {file.filename}")
            continue

        # 1. Save uploaded file
        file_path = os.path.join(upload_dir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 2. Determine output directory
        base_name = os.path.splitext(file.filename)[0]
        output_dir_name = f"{base_name}_data"
        output_dir = os.path.join(BOOKS_DIR, output_dir_name)

        # 3. Process and Convert
        try:
            print(f"Processing uploaded file: {file.filename}...")
            book_obj = process_epub(file_path, output_dir)
            save_to_pickle(book_obj, output_dir)
        except Exception as e:
            print(f"Error processing book {file.filename}: {e}")
            continue

    # Clear cache once after all books are processed
    load_book_cached.cache_clear()

    # 4. Redirect to library
    return RedirectResponse(url="/", status_code=303)

@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_first_chapter(book_id: str):
    """Helper to just go to chapter 0."""
    return await read_chapter(book_id=book_id, chapter_index=0)

@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Update last opened history
    update_history(book_id)

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    current_chapter = book.spine[chapter_index]

    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    return templates.TemplateResponse("reader.html", {
        "request": request,
        "book": book,
        "current_chapter": current_chapter,
        "chapter_index": chapter_index,
        "book_id": book_id,
        "prev_idx": prev_idx,
        "next_idx": next_idx
    })

def flatten_toc_with_depth(entries: List[TOCEntry], depth=0) -> List[Tuple[TOCEntry, int]]:
    result = []
    for entry in entries:
        result.append((entry, depth))
        if entry.children:
            result.extend(flatten_toc_with_depth(entry.children, depth + 1))
    return result

@app.get("/api/content/recursive/{book_id}")
async def get_chapter_content_recursive(book_id: str, href: str):
    """
    Returns the concatenated content of a chapter and all its sub-chapters.
    Identifies the start and end spine indices based on TOC structure.
    """
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # 1. Build Spine Map (href -> index)
    spine_map = {ch.href: i for i, ch in enumerate(book.spine)}

    # 2. Flatten TOC to find structure
    flat_toc = flatten_toc_with_depth(book.toc)
    
    # 3. Find target entry index in flat_toc
    target_idx = -1
    target_entry = None
    target_depth = -1
    
    for i, (entry, depth) in enumerate(flat_toc):
        if entry.href == href:
            target_idx = i
            target_entry = entry
            target_depth = depth
            break
            
    if target_idx == -1:
        # Fallback: if href not in TOC (maybe weird mismatch), try direct spine lookup
        # If user clicked something that isn't in TOC tree?
        raise HTTPException(status_code=404, detail="Chapter not found in TOC")

    # 4. Find End Entry (Next Sibling/Uncle)
    # Scan forward until we find an entry with depth <= target_depth
    end_entry_href = None
    
    for i in range(target_idx + 1, len(flat_toc)):
        entry, depth = flat_toc[i]
        if depth <= target_depth:
            end_entry_href = entry.href
            break
            
    # 5. Determine Spine Indices
    # Start Index
    start_spine_idx = spine_map.get(href)
    if start_spine_idx is None:
        # Fallback: try finding first spine item starting with href#
        prefix = href + "#"
        for h, idx in spine_map.items():
            if h.startswith(prefix):
                start_spine_idx = idx
                break
                
    if start_spine_idx is None:
         raise HTTPException(status_code=404, detail="Content not found in spine")

    # End Index
    if end_entry_href:
        end_spine_idx = spine_map.get(end_entry_href)
        if end_spine_idx is None:
             # Fallback: try finding first spine item starting with end_entry_href#
            prefix = end_entry_href + "#"
            for h, idx in spine_map.items():
                if h.startswith(prefix):
                    end_spine_idx = idx
                    break
        
        # If still None, it means the next chapter has no content? Use len(spine)
        if end_spine_idx is None:
             end_spine_idx = len(book.spine)
    else:
        # No next sibling, go to end
        end_spine_idx = len(book.spine)
        
    # Safety clamp
    if end_spine_idx < start_spine_idx:
        end_spine_idx = len(book.spine)

    # 6. Collect Content
    content_parts = []
    for i in range(start_spine_idx, end_spine_idx):
        content_parts.append(book.spine[i].content)
        
    full_content = "\n<hr class='chapter-divider'>\n".join(content_parts)
    
    return {"content": full_content}


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)

if __name__ == "__main__":
    import uvicorn
    # 修改提示信息，0.0.0.0 代表监听所有地址
    print("Starting server at http://0.0.0.0:8123")
    # 将 host 改为 "0.0.0.0" 以允许外部访问
    uvicorn.run(app, host="0.0.0.0", port=8123)
