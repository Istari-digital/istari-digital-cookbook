import os
import sys
import nbformat
from nbclient import NotebookClient, CellExecutionError

def find_notebooks(root="."):
    """Find all .ipynb files excluding .ipynb_checkpoints"""
    notebooks = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip checkpoint directories
        dirnames[:] = [d for d in dirnames if d != ".ipynb_checkpoints"]
        for filename in filenames:
            if filename.endswith(".ipynb"):
                notebooks.append(os.path.join(dirpath, filename))
    return notebooks

def run_notebook(path, timeout=600, save_output=False):
    print(f"🔍 Running {path}")
    try:
        with open(path, encoding="utf-8") as f:
            nb = nbformat.read(f, as_version=4)

        client = NotebookClient(nb, timeout=timeout)
        client.execute()

        if save_output:
            with open(path, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)

        print(f"✅ Finished {path}")
        return True

    except CellExecutionError as e:
        print(f"❌ Execution failed in {path}:\n{e}")
        return False

    except Exception as e:
        print(f"❌ Unexpected error in {path}:\n{e}")
        return False

def main():
    notebooks = find_notebooks()
    print(f"Found {len(notebooks)} notebooks.")

    all_passed = True
    for nb in notebooks:
        success = run_notebook(nb)
        if not success:
            all_passed = False

    if not all_passed:
        print("❗ One or more notebooks failed.")
        sys.exit(1)

    print("✅ All notebooks ran successfully.")

if __name__ == "__main__":
    main()
