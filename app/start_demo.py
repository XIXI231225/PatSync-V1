from pathlib import Path
import os,sys,traceback
ROOT=Path(__file__).resolve().parent
def launch():
    os.chdir(ROOT);sys.dont_write_bytecode=True
    from database import PatentDatabase
    PatentDatabase(ROOT/'data/patents.db')
    os.environ['DEMO_PATENT_PORT']=os.getenv('DEMO_PATENT_PORT','18879')
    from server import main
    main()
if __name__=='__main__':
    try:launch()
    except KeyboardInterrupt:pass
    except Exception:
        detail=traceback.format_exc();(ROOT/'启动错误.log').write_text(detail,encoding='utf-8');print(detail,file=sys.stderr);sys.exit(1)
