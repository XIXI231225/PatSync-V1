"""General entrypoint with no bundled samples or preloaded records."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
sys.dont_write_bytecode=True

def main():
    p=argparse.ArgumentParser(description='专利总表维护助手')
    commands=p.add_subparsers(dest='command',required=True)
    commands.add_parser('serve');commands.add_parser('status')
    command=commands.add_parser('import');command.add_argument('bundle',type=Path)
    args=p.parse_args()
    if args.command=='serve':
        from start_demo import launch
        return launch()
    from database import PatentDatabase
    db=PatentDatabase(ROOT/'app/data/patents.db')
    if args.command=='status':result=db.stats()
    else:
        from bundle_transfer import import_bundle
        result=import_bundle(args.bundle,db,ROOT/'app/data')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        sys.exit(1)
