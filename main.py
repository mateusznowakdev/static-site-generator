import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, UTC
from pathlib import Path

import click
from jinja2 import Environment, FileSystemLoader
from mistune import create_markdown
from mistune.directives import RSTDirective, TableOfContents
from mistune.plugins.formatting import strikethrough, superscript
from mistune.plugins.table import table
from mistune.renderers.html import HTMLRenderer
from mistune.util import escape, safe_entity, striptags
from PIL import Image
from slugify import slugify
from yaml import safe_load

RE_FRONTMATTER = re.compile(
    r"^((-{3,})\n(?P<custom>.*?)\n(-{3,})\n)?(?P<content>.*)$", re.DOTALL
)
RE_EXTRACT_IMAGES = re.compile(r"!\[.*?]\((?P<filename>.*?)\)")


@dataclass
class Site:
    name: str = ""
    author: str = ""
    domain: str = ""
    pages: list = field(default_factory=list)


@dataclass
class Page:
    # from contents.yml
    url: str = ""
    title: str = ""
    description: str = ""
    banner_id: str = ""
    type: str = ""
    date: date = None
    sitemap: bool = True
    # from page
    content: str = ""
    custom: dict = None
    # auto-generated
    src: str = ""
    dst: str = ""


class CustomRenderer(HTMLRenderer):
    def link(self, text, url, title=None):
        # added rel and target
        s = '<a href="' + self.safe_url(url) + '"'
        if title:
            s += ' title="' + safe_entity(title) + '"'
        if "://" in url:
            s += ' rel="noopener" target="_blank"'
        return s + ">" + text + "</a>"

    def image(self, text, url, title=None):
        # added srcset and loading, wrapped into clickable figure with figcaption
        url = self.safe_url(url)
        stem, ext = os.path.splitext(url)
        alt = striptags(text)
        s = '<img src="' + stem + '-w1024.webp" alt="' + alt + '"'
        s += ' title="' + safe_entity(title or alt) + '"'
        s += ' loading="lazy" />'
        s = self.link(s, stem + '.webp')
        return "</p><figure>" + s + "<figcaption>" + alt + "</figcaption></figure><p>"

    def table(self, text):
        return '<div class="table-wrapper"><table>' + text + "</table></div>"


class CustomTableOfContents(TableOfContents):
    def generate_heading_id(self, token, index):
        return slugify(token["text"])


def get_template(template_file):
    env = Environment(
        loader=FileSystemLoader(template_file.parent),
        lstrip_blocks=True,
        trim_blocks=True,
    )
    tpl = env.get_template(template_file.name)
    return tpl


def copy_source_to_target(source_dir, target_dir):
    shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)


def collect_frontmatter(raw_content):
    match = RE_FRONTMATTER.match(raw_content)

    custom = match.group("custom") or ""
    custom = safe_load(custom) or {}
    content = match.group("content")

    return {"custom": custom, "content": content}


def parse_config(target_dir, config_file):
    with open(config_file) as config:
        site = safe_load(config)

    site = Site(**site)
    pages = site.pages

    for page_data in pages:
        path = Path(target_dir / page_data["url"].lstrip("/"))
        if path.suffix == ".html":
            src = path.with_suffix(".md")
        else:
            src = path / "index.md"
        page_data["src"] = src if src.is_file() else None

        if page_data["src"]:
            page_data["dst"] = page_data["src"].with_suffix(".html")

            with open(page_data["src"], "r") as src:
                content = src.read()
                frontmatter_and_content = collect_frontmatter(content)
                page_data.update(**frontmatter_and_content)

    pages = [Page(**p) for p in pages]
    pages = sorted(
        pages,
        key=lambda p: p.date or datetime(1970, 1, 1, 0, 0, tzinfo=UTC),
        reverse=True,
    )
    site.pages = pages

    return site


def calculate_crop(image, gravity):
    width, height = image.size
    diff = width - height

    if diff > 0:  # horizontal
        if gravity == "start":
            crop = (0, 0, height, height)
        elif gravity == "end":
            crop = (diff, 0, diff + height, height)
        else:
            crop = (diff // 2, 0, diff // 2 + height, height)
    elif diff < 0:  # vertical
        if gravity == "start":
            crop = (0, 0, width, width)
        elif gravity == "end":
            crop = (0, -diff, width, -diff + width)
        else:
            crop = (0, -diff // 2, width, -diff // 2 + width)
    else:  # square
        crop = (0, 0, width, height)

    return crop


def generate_article_thumbnails(image_file):
    with Image.open(image_file) as img:
        img.thumbnail((min(img.width, 1920), 9999))
        img.save(image_file.parent / f"{image_file.stem}.webp", quality=90)
        img.thumbnail((1024, 9999))
        img.save(image_file.parent / f"{image_file.stem}-w1024.webp", quality=90)

    image_file.unlink()



def generate_banner_thumbnails(image_file, gravity):
    with Image.open(image_file) as img:
        img.thumbnail((min(img.width, 1920), 9999))
        img.save(image_file.parent / f"{image_file.stem}.webp", quality=90)
        img = img.crop(calculate_crop(img, gravity))
        img.thumbnail((720, 720))
        img.save(image_file.parent / f"{image_file.stem}-w720.webp", quality=90)

    image_file.unlink()



def transform_pages(site):
    markdown = create_markdown(
        renderer=CustomRenderer(escape=False),
        plugins=(
            RSTDirective([CustomTableOfContents()]),
            strikethrough,
            superscript,
            table
        ),
    )

    for page in site.pages:
        if page.content:
            parent_dir = page.src.parent

            extracted_image_files = RE_EXTRACT_IMAGES.findall(page.content)
            for rel_file in extracted_image_files:
                file = parent_dir / rel_file
                try:
                    generate_article_thumbnails(file)
                except FileNotFoundError:
                    pass

        page.content = markdown(page.content)

        if page.custom:
            parent_dir = page.src.parent

            for feature in page.custom.get("features") or {}:
                banner = feature.get("banner")
                banner_gravity = feature.get("banner_gravity")
                if not banner:
                    continue

                file = parent_dir / banner
                try:
                    generate_banner_thumbnails(file, banner_gravity)
                except FileNotFoundError:
                    pass


def get_page_context(site, page):
    return {"site": site, "page": page}


def export_pages(site, templates_dir):
    template = get_template(templates_dir / "page.html")

    for page in site.pages:
        if not page.src:
            continue

        with open(page.dst, "w") as dst:
            context = get_page_context(site, page)
            output = template.render(**context)
            dst.write(output)

        page.src.unlink()


def get_sitemap_context(site):
    return {"site": site}


def export_sitemap(site, templates_dir, output_dir):
    template = get_template(templates_dir / "sitemap.xml")

    with open(output_dir / "sitemap.xml", "w") as dst:
        context = get_sitemap_context(site)
        output = template.render(**context)
        dst.write(output)


def get_feed_context(site):
    return {"site": site, "now": datetime.now(UTC)}


def export_feed(site, templates_dir, output_dir):
    template = get_template(templates_dir / "atom.xml")

    with open(output_dir / "atom.xml", "w") as dst:
        context = get_feed_context(site)
        output = template.render(**context)
        dst.write(output)


@click.command()
@click.argument("workspace", type=click.Path(exists=True, file_okay=False))
def main(workspace):
    workspace_dir = Path(workspace).resolve()

    input_dir = workspace_dir / "input"
    output_dir = workspace_dir / "output"
    templates_dir = workspace_dir / "templates"
    config_file = workspace_dir / "config.yml"

    copy_source_to_target(input_dir, output_dir)

    site = parse_config(output_dir, config_file)
    transform_pages(site)

    export_pages(site, templates_dir)
    export_sitemap(site, templates_dir, output_dir)
    export_feed(site, templates_dir, output_dir)


if __name__ == "__main__":
    main()
