"""Functional tests over actual HTTP using an isolated SQLite file and cookie jars."""
import http.cookiejar, json, socket, tempfile, threading, unittest, urllib.request, urllib.error
from pathlib import Path
import server
from fixtures import EXAMPLE

class Client:
    def __init__(self,base):
        self.base=base;self.agent=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(self,path,data=None):
        req=urllib.request.Request(self.base+"/api/"+path,data=json.dumps(data).encode() if data is not None else None,headers={"Content-Type":"application/json"})
        with self.agent.open(req,timeout=5) as r:return json.load(r)

class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=server.DB;server.DB=Path(self.tmp.name)/"test.sqlite3";server.init()
        self.host=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler);self.thread=threading.Thread(target=self.host.serve_forever,daemon=True);self.thread.start()
        self.base=f"http://127.0.0.1:{self.host.server_port}";self.owner=Client(self.base);self.other=Client(self.base);self.reader=Client(self.base)
        self.owner.call("workspace");self.other.call("workspace");self.pid=self.owner.call("projects",EXAMPLE)["projectId"];self.path="projects/"+self.pid
    def tearDown(self):
        self.host.shutdown();self.host.server_close();self.thread.join();server.DB=self.old;self.tmp.cleanup()
    def err(self,status,fn,*args):
        with self.assertRaises(urllib.error.HTTPError) as caught:fn(*args)
        self.assertEqual(caught.exception.code,status);caught.exception.close()
    def pub(self):
        run=self.owner.call(self.path+"/runs",{"requestId":"demo-1"})
        draft=dict(runId=run["runId"],candidateId="interview",title="试用拾句",material="脱敏逐字稿",steps="准备材料\n查找引文",criterion="定位到原文",question="哪一句有效？")
        return draft,self.owner.call(self.path+"/publish",draft)
    def test_match_evidence_and_empty_error(self):
        r=self.owner.call(self.path+"/runs",{"requestId":"first"});self.assertEqual(r,self.owner.call(self.path+"/runs",{"requestId":"first"}))
        self.assertEqual(r,self.owner.call(self.path+"/runs/"+r["runId"]))
        self.assertEqual([x["judgment"] for x in r["candidates"]],["适配","排除"])
        for x in r["candidates"]:self.assertIn(x["evidence"],self.owner.call("sources/"+x["id"])["body"])
        self.owner.call("projects",dict(EXAMPLE,projectId=self.pid,material="盆栽照片",task="辨认花卉",output="花名",limits=""))
        self.assertEqual(self.owner.call(self.path+"/runs",{"requestId":"empty"})["status"],"empty")
        self.assertEqual(self.owner.call(self.path+"/runs",{"requestId":"live","mode":"live"})["errorCode"],"not_configured")
    def test_ownership_snapshots_and_withdrawal(self):
        d,c=self.pub();s=c["slug"];self.assertEqual(c,self.owner.call(self.path+"/publish",d))
        self.err(404,self.other.call,self.path);self.err(404,self.other.call,self.path+"/results");self.err(404,self.other.call,self.path+"/publish",d)
        self.err(400,self.owner.call,self.path+"/publish",dict(d,candidateId="transcribe"))
        nextcard=self.owner.call(self.path+"/publish",dict(d,title="新版"));self.assertEqual(nextcard["version"],2)
        self.assertEqual(self.reader.call("cards/"+s)["title"],"试用拾句");self.assertNotIn("owner",c);self.assertNotIn("projectSnapshot",c)
        self.owner.call("projects",dict(EXAMPLE,projectId=self.pid,output="修改后的产物"));self.err(409,self.owner.call,self.path+"/publish",d)
        self.owner.call(self.path+"/withdraw/"+s,{});self.err(404,self.reader.call,"cards/"+s);self.assertEqual(self.reader.call("cards/"+nextcard["slug"])["title"],"新版")
    def test_reader_counts_receipt_and_isolation(self):
        _,c=self.pub();p="cards/"+c["slug"];self.reader.call(p)
        for event in ["visit","visit","open","open"]:self.reader.call(p+"/"+event,{"version":1})
        stats=lambda:self.owner.call(self.path+"/results")["versions"][0]
        self.assertEqual([stats()[k] for k in ("visits","opened","completed")],[1,1,0])
        f=dict(version=1,submissionId="try-1",status="blocked",step="核对第三句",note="候选句讲的是安排时间。")
        self.err(409,self.reader.call,p+"/feedback",dict(f,version=2));self.err(400,self.reader.call,p+"/feedback",dict(f,step=""))
        a=self.reader.call(p+"/feedback",f);self.assertEqual(a,self.reader.call(p+"/feedback",f));self.assertEqual(a,self.reader.call(p+"/visit",{"version":1}))
        self.assertEqual(len(stats()["feedback"]),1);self.assertEqual(stats()["completed"],0)
        self.other.call(p);self.other.call(p+"/feedback",dict(f,submissionId="another",status="done",step=""))
        self.assertEqual((stats()["visits"],stats()["opened"],stats()["completed"],len(stats()["feedback"])),(2,1,1,2))
    def test_saved_project_and_safe_tool_entry(self):
        self.assertEqual(self.owner.call("workspace")["projects"][0]["task"],EXAMPLE["task"]);self.assertEqual(self.other.call("workspace")["projects"],[])
        self.err(400,self.owner.call,"projects",dict(EXAMPLE,url="data:text/plain,no"))
        self.assertTrue(server.DB.is_file());server.init();self.assertEqual(self.owner.call(self.path)["project"]["name"],EXAMPLE["name"])
    def test_early_4xx_consumes_large_body(self):
        # A 512 KiB body exceeds the loopback socket buffers, so an early
        # 413/415 that replies without reading the body resets the connection
        # and the client gets IncompleteRead instead of the status code.
        def send(headers):
            req=urllib.request.Request(self.base+"/api/projects",data=b"x"*(512*1024),headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as caught:self.owner.agent.open(req,timeout=10)
            self.assertEqual(caught.exception.code,expect);caught.exception.close()
        expect=413;send({"Content-Type":"application/json"})
        expect=415;send({"Content-Type":"text/plain"})
        # Raw socket proves the body is consumed before the early error is
        # written: with the old code the 413 arrives while only 100 of the
        # declared 30000 bytes have been sent.
        with socket.create_connection(("127.0.0.1",self.host.server_port),timeout=5) as sock:
            head=(f"POST /api/projects HTTP/1.1\r\nHost: 127.0.0.1:{self.host.server_port}\r\n"
                  "Content-Type: application/json\r\nContent-Length: 30000\r\n\r\n").encode()
            sock.sendall(head+b"x"*100)
            sock.settimeout(0.4)
            try:early=sock.recv(64)
            except socket.timeout:early=b""
            self.assertFalse(early,"服务端未读完请求体就提前返回错误")
            sock.settimeout(5)
            sock.sendall(b"x"*(30000-100))
            self.assertIn(b" 413 ",sock.recv(256).split(b"\r\n")[0])

if __name__=="__main__":unittest.main(verbosity=2)
