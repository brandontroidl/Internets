# Sphinx build configuration for the Internets documentation.
#
# Two outputs from one source tree:
#   * HTML  -> docs/_build/html/         (sphinx-build -b html)
#   * PDF   -> docs/_build/latex/*.pdf   (sphinx-build -b latex, then xelatex)
#
# The narrative guides under docs/*.md are authored in Markdown (rendered by
# myst-parser).  The API reference is produced by sphinx-autoapi, which parses
# the source statically -- it never imports the bot, so side-effectful imports
# and optional heavy dependencies cannot break the build.
#
# Docs: https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information ------------------------------------------------------
# Kept in sync with pyproject.toml (the canonical source of truth).
project = "Internets"
author = "Brandon Troidl"
copyright = "2026, Brandon Troidl"
release = "5.0.0"
version = "5.0"

# -- General configuration ----------------------------------------------------
extensions = [
    "myst_parser",                    # Markdown (.md) sources
    "autoapi.extension",              # static API reference from source
    "sphinx.ext.napoleon",            # Google/NumPy docstring sections
    "sphinx.ext.intersphinx",         # cross-link to the Python stdlib docs
    "sphinx.ext.graphviz",            # render graphviz (dot) diagrams
    "sphinx.ext.inheritance_diagram", # class inheritance graphs
    "sphinx_copybutton",              # copy button on code blocks
    "sphinx_design",                  # grids/cards/tabs for the landing page
]

root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Intersphinx: link identifiers like ``asyncio.Task`` to the stdlib docs.
intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

# -- MyST (Markdown) ----------------------------------------------------------
myst_enable_extensions = [
    "colon_fence",   # ::: fenced directives
    "deflist",       # definition lists
    "fieldlist",     # field lists
    "tasklist",      # - [ ] checkboxes
    "attrs_block",   # block attributes
    "attrs_inline",  # inline attributes
    "substitution",  # {{ substitutions }}
]
# Auto-generate anchors down to <h4> so intra- and cross-doc heading links
# in the existing guides resolve.
myst_heading_anchors = 4
# The guides carry standalone H1 titles; do not warn on the resulting
# document-title / first-heading conventions.
suppress_warnings = ["myst.header"]

# -- sphinx-autoapi -----------------------------------------------------------
autoapi_type = "python"
# Scan the repository root; everything non-source is filtered by autoapi_ignore.
autoapi_dirs = [".."]
autoapi_root = "autoapi"
autoapi_ignore = [
    "*/tests/*",
    "*/.git/*",
    "*/_build/*",
    "*/build/*",
    "*/dist/*",
    "*/docs/*",
    "*/scripts/*",
    "*/__pycache__/*",
    "*/.pytest_cache/*",
    "*.egg-info/*",
    "*/conf.py",
    "*/setup.py",
]
# Complete internal reference: document undocumented members AND private
# ``_name`` modules/members (the SSRF guard, the provider dispatch/health/http
# framework, and the weather-code tables are internal-by-convention but are
# exactly what this manual is for).  Inheritance shown.
autoapi_options = [
    "members",
    "undoc-members",
    "private-members",
    "show-inheritance",
    "show-module-summary",
]
autoapi_python_class_content = "both"   # class docstring + __init__ docstring
autoapi_member_order = "groupwise"
autoapi_add_toctree_entry = True        # generate + link the API Reference landing
autoapi_keep_files = False              # regenerated each build

# -- Napoleon -----------------------------------------------------------------
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = False

# -- HTML output --------------------------------------------------------------
html_theme = "sphinx_rtd_theme"
html_title = "Internets 5.0.0"
html_short_title = "Internets"
html_static_path = []

# -- LaTeX / PDF output -------------------------------------------------------
# xelatex, not pdflatex: the guides contain Unicode (degrees, arrows,
# box-drawing) that pdflatex cannot typeset without extra input encoding.
latex_engine = "xelatex"
latex_documents = [
    ("index", "internets.tex", "Internets Documentation", "Brandon Troidl", "manual"),
]
latex_elements = {
    "papersize": "letterpaper",
    "pointsize": "10pt",
    # Force very long code/verbatim lines to wrap rather than overflow the
    # margin (the API pages and config examples have long lines).
    "sphinxsetup": "verbatimforcewraps=true",
}
