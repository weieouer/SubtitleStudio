// 每个标签页保留独立草稿；服务端确认保存后只清理本页对应版本。
(() => {
  let connection, writes=Promise.resolve();
  // 每次页面实例独立，浏览器“复制标签页”也不会继承同一写入键。
  const tab=crypto.randomUUID();
  const key=source=>JSON.stringify([source,tab]);
  function open() {
    if(connection)return connection;
    connection=new Promise((resolve,reject)=>{
      let settled=false;
      const timer=setTimeout(()=>{settled=true;reject(new Error('草稿存储打开超时'));},5000);
      const request=indexedDB.open('subtitleStudio.drafts',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('drafts',{keyPath:'key'});
      request.onsuccess=()=>{
        clearTimeout(timer);
        if(settled){request.result.close();return;}
        settled=true;
        const db=request.result;db.onversionchange=()=>{db.close();connection=null;};resolve(db);
      };
      request.onerror=()=>{clearTimeout(timer);settled=true;reject(request.error);};
      request.onblocked=()=>{clearTimeout(timer);settled=true;reject(new Error('草稿存储被其他页面占用'));};
    }).catch(error=>{connection=null;throw error;});
    return connection;
  }
  async function transaction(mode, run) {
    const db=await open();
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('drafts',mode);let value;
      tx.oncomplete=()=>resolve(value);
      tx.onerror=tx.onabort=()=>reject(tx.error||new Error('草稿写入失败'));
      run(tx.objectStore('drafts'),result=>{value=result;});
    });
  }
  function queue(run){const task=writes.then(run);writes=task.catch(()=>{});return task;}
  window.SubtitleDrafts={
    put(source,payload,editRevision,baseRevision){
      const record={key:key(source),source,tab,version:crypto.randomUUID(),payload:JSON.parse(JSON.stringify(payload)),editRevision,baseRevision,updatedAt:Date.now()};
      return queue(()=>transaction('readwrite',(store,done)=>{store.put(record);done(record.key);}));
    },
    clearThrough(source,revision,baseRevision){return queue(()=>transaction('readwrite',(store,done)=>{
      const request=store.get(key(source));
      request.onsuccess=()=>{
        const record=request.result;if(!record)return;
        if(record.editRevision<=revision)store.delete(key(source));
        else if(baseRevision!=null){record.baseRevision=baseRevision;store.put(record);}
        const count=store.count();count.onsuccess=()=>done(count.result);
      };
    }));},
    clearRecovered(id,version){return queue(()=>transaction('readwrite',store=>{
      const r=store.get(id);r.onsuccess=()=>{if(r.result&&(r.result.version??r.result.updatedAt)===version)store.delete(id);};
    }));},
    async list(){await writes;return transaction('readonly',(store,done)=>{const r=store.getAll();r.onsuccess=()=>done(r.result.sort((a,b)=>b.updatedAt-a.updatedAt));});},
    async get(id){await writes;return transaction('readonly',(store,done)=>{const r=store.get(id);r.onsuccess=()=>done(r.result);});},
    discard(id){return queue(()=>transaction('readwrite',store=>store.delete(id)));}
  };
})();
