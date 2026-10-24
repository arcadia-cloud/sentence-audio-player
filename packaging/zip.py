import os, zipfile, pathlib, time
src = pathlib.Path(r"D:\Users\Lenovo\Documents\deepseek-harness\default-workspace\listening-app\dist\ListeningStudio")
out = pathlib.Path(r"D:\Users\Lenovo\Documents\deepseek-harness\default-workspace\listening-app\dist\ListeningStudio-portable.zip")
t0=time.time()
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
    for root, dirs, files in os.walk(src):
        for f in files:
            p = pathlib.Path(root)/f
            arc = pathlib.Path("ListeningStudio")/p.relative_to(src)
            z.write(p, arc)
print("done", out.stat().st_size, round(time.time()-t0,1))
