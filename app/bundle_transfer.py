"""Import a self-contained ledger bundle into an empty local database."""
from pathlib import Path, PurePosixPath
import hashlib,json,os,shutil,tempfile,uuid,zipfile
from datetime import datetime
from core import normalize_application_no,configure_organization

TABLES=('patents','source_documents','materials')
MAX_BYTES=512*1024*1024

def safe_name(name):
    p=PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or any(x in ('','..','.') for x in name.split('/')):
        raise ValueError('数据包包含不安全路径')
    return p

def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def validate_organization(value):
    if not isinstance(value,dict) or set(value)-{'group_aliases','group_applicants','group_search_terms','company_aliases'}:
        raise ValueError('组织查询映射格式错误')
    for key in ('group_aliases','group_applicants','group_search_terms'):
        items=value.get(key,[])
        if not isinstance(items,list) or len(items)>1000 or any(not isinstance(x,str) or not x.strip() or len(x)>500 for x in items):
            raise ValueError('组织查询映射格式错误')
    aliases=value.get('company_aliases',{})
    if not isinstance(aliases,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not k or not v for k,v in aliases.items()):
        raise ValueError('组织查询别名格式错误')
    return value

def import_bundle(bundle,db,data_dir):
    bundle=Path(bundle);data=Path(data_dir).resolve()
    if db.path.resolve().parent!=data:raise ValueError('数据库与目标数据目录不一致')
    if bundle.stat().st_size>MAX_BYTES:raise ValueError('数据包超过512MB')
    bundle_id=digest_file(bundle)
    data.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bundle-stage-',dir=data) as temporary:
        stage=Path(temporary)
        try:
            with zipfile.ZipFile(bundle) as archive:
                entries=archive.infolist()
                names=[i.filename for i in entries]
                if len(names)>5000 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>MAX_BYTES:
                    raise ValueError('数据包文件数量、大小或重复路径异常')
                for item in entries:
                    safe_name(item.filename)
                    if item.is_dir() or item.flag_bits&1:raise ValueError('数据包不支持目录条目或加密文件')
                if 'manifest.json' not in names:raise ValueError('缺少完整数据包清单，请勿上传普通材料ZIP')
                if archive.getinfo('manifest.json').file_size>2*1024*1024:raise ValueError('数据清单过大')
                manifest=json.loads(archive.read('manifest.json').decode('utf-8'))
                if manifest.get('format')!='patent-ledger-bundle' or manifest.get('format_version')!=1:
                    raise ValueError('数据包格式或版本不支持')
                metadata=manifest.get('files')
                if not isinstance(metadata,list):raise ValueError('缺少文件校验清单')
                mapping={}
                for item in metadata:
                    name=item.get('path','');safe_name(name)
                    if name in mapping or name=='manifest.json':raise ValueError('数据清单重复')
                    if type(item.get('bytes')) is not int or item['bytes']<0 or not isinstance(item.get('sha256'),str):
                        raise ValueError('文件校验清单格式错误')
                    mapping[name]=item
                required={t+'.json' for t in TABLES}|{'organization.json'}
                if not required.issubset(mapping) or set(names)!=(set(mapping)|{'manifest.json'}):raise ValueError('清单与实际文件不一致')
                for name,item in mapping.items():
                    if name not in required and not (name.startswith('files/materials/') or name.startswith('files/sources/')):
                        raise ValueError('数据包包含不支持的文件')
                    info=archive.getinfo(name)
                    if info.file_size!=item['bytes']:raise ValueError('文件大小校验失败')
                    target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
                    h=hashlib.sha256()
                    with archive.open(info) as source,target.open('wb') as dest:
                        for block in iter(lambda:source.read(1024*1024),b''):dest.write(block);h.update(block)
                    if h.hexdigest()!=item['sha256']:raise ValueError('文件SHA-256校验失败，未导入')
        except (zipfile.BadZipFile,KeyError,TypeError,json.JSONDecodeError,UnicodeError) as exc:
            raise ValueError('数据包损坏或内容格式错误，未导入') from exc

        tables={name:json.loads((stage/(name+'.json')).read_text('utf-8')) for name in TABLES}
        organization=validate_organization(json.loads((stage/'organization.json').read_text('utf-8')))
        counts=manifest.get('counts',{})
        ids={}
        with db.connect() as conn:
            for table,rows in tables.items():
                if not isinstance(rows,list) or counts.get(table)!=len(rows):raise ValueError('记录数量与清单不一致')
                columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
                ids[table]=set()
                for row in rows:
                    if not isinstance(row,dict) or set(row)-columns or type(row.get('id')) is not int or row['id']<=0:
                        raise ValueError('台账记录字段或ID格式错误')
                    if row['id'] in ids[table]:raise ValueError('台账记录ID重复')
                    ids[table].add(row['id'])
            keys=set()
            for row in tables['patents']:
                key=normalize_application_no(row.get('application_no'))
                if not key or key!=row.get('app_key') or key in keys:raise ValueError('申请号为空、重复或与匹配键不一致')
                keys.add(key)
                raw=json.loads(row.get('raw_json','{}'))
                if not isinstance(raw,dict):raise ValueError('原台账字段格式错误')
            references=set()
            for table in ('materials','source_documents'):
                for row in tables[table]:
                    relative=str(row.get('stored_path',''));safe_name(relative)
                    item=mapping.get('files/'+relative)
                    if not item or row.get('sha256')!=item['sha256']:raise ValueError('材料引用、原件或哈希校验失败')
                    references.add('files/'+relative)
                    if table=='materials':
                        if row.get('patent_id') not in ids['patents']:raise ValueError('材料关联的专利不存在')
                        if row.get('source_document_id') is not None and row['source_document_id'] not in ids['source_documents']:
                            raise ValueError('材料关联的来源原件不存在')
            if {n for n in mapping if n.startswith('files/')}!=references:raise ValueError('存在无关联材料，未导入')

        destination=data/'bundle-files'/bundle_id
        organization_file=data/'organization.json'
        old_organization=organization_file.read_bytes() if organization_file.exists() else None
        created=False;organization_written=False
        with db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('CREATE TABLE IF NOT EXISTS imported_bundles(bundle_id TEXT PRIMARY KEY,imported_at TEXT NOT NULL,counts_json TEXT NOT NULL)')
                previous=conn.execute('SELECT counts_json FROM imported_bundles WHERE bundle_id=?',(bundle_id,)).fetchone()
                if previous:
                    original=json.loads(previous[0])
                    return {'already_imported':True,'bundle_id':bundle_id,**original}
                if any(conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in TABLES):
                    raise ValueError('当前库已有台账或材料，请使用空白Skill副本首次导入；本次未覆盖原数据')
                if destination.exists():raise ValueError('存在未完成的同包文件，请使用新的空白目录导入')
                destination.parent.mkdir(exist_ok=True)
                (stage/'files').mkdir(exist_ok=True)
                os.replace(stage/'files',destination);created=True
                for table in TABLES:
                    for original in tables[table]:
                        row=dict(original)
                        if table in ('materials','source_documents'):
                            row['stored_path']=(Path('bundle-files')/bundle_id/row['stored_path']).as_posix()
                        columns=list(row)
                        conn.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[row[k] for k in columns])
                temp_organization=data/('organization-'+uuid.uuid4().hex+'.tmp')
                temp_organization.write_text(json.dumps(organization,ensure_ascii=False,indent=2),encoding='utf-8')
                os.replace(temp_organization,organization_file);organization_written=True
                result={'patents':len(tables['patents']),'materials':len(tables['materials']),'source_documents':len(tables['source_documents'])}
                conn.execute('INSERT INTO imported_bundles VALUES(?,?,?)',(bundle_id,datetime.now().isoformat(timespec='seconds'),json.dumps(result)))
                conn.execute('INSERT INTO operations(created_at,operation,result,message) VALUES(?,?,?,?)',
                    (datetime.now().isoformat(timespec='seconds'),'整包导入','成功',f"专利{result['patents']}件，材料{result['materials']}条，来源原件{result['source_documents']}份"))
                conn.commit()
            except Exception:
                conn.rollback()
                if created and destination.resolve().is_relative_to((data/'bundle-files').resolve()):shutil.rmtree(destination)
                if organization_written:
                    if old_organization is None:organization_file.unlink(missing_ok=True)
                    else:organization_file.write_bytes(old_organization)
                raise
        configure_organization(organization)
        return {'already_imported':False,'bundle_id':bundle_id,**result}
