const $ = id => document.getElementById(id);
const labels = {baseline:'建立历史基线',new_post:'发现新帖',updated:'帖子有编辑',unlocked:'已取得会员正文',access_changed:'读取权限变化',monitor_error:'采集异常',recovered:'采集已恢复',video_baseline:'建立视频历史基线',video_new:'新视频上线',video_updated:'视频信息更新',video_download_requested:'请求下载视频',video_file_ready:'视频已下载'};
const formatTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Singapore',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)) : '—';
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(cls) el.className=cls; return el; }
function postCard(post) {
  const card=node('article',undefined,'post');
  const head=node('div',undefined,'post-head');
  head.append(node('strong',`华尔街没有名字 · #${post.id}`),node('time',formatTime(post.createdAt)));
  card.append(head);
  if(post.locked) card.append(node('div',`会员内容受限 · ${post.textLength} 字 · ${post.images.length} 张配图。当前未取得正文。`,'locked'));
  else card.append(node('div',post.text,'post-body'));
  if(post.cachedText!==null) {
    card.append(node('small',`以下为 ${formatTime(post.cachedAt)} 保存的历史正文，可能与当前版本不同。`,'cached'));
    card.append(node('div',post.cachedText,'post-body cached'));
  }
  if(!post.locked&&post.images.length) {
    const gallery=node('div',undefined,'post-gallery');
    post.images.forEach((image,index)=>{
      if(image.localUrl) {
        const link=node('a',undefined,'original-image');
        link.href=image.localUrl;link.target='_blank';link.rel='noopener noreferrer';
        const picture=node('img');picture.src=image.localUrl;picture.alt=`帖子 #${post.id} 原图 ${index+1}`;picture.loading='lazy';
        link.append(picture,node('small',`原图 ${index+1} · ${Math.round(image.size/1024)} KB · 点击放大`));gallery.append(link);
      } else gallery.append(node('p',`配图 ${index+1}：${image.downloadStatus==='failed'?'下载失败，将自动重试':'等待下载原图'}`,'image-pending'));
    });
    card.append(gallery);
  }
  const foot=node('div',undefined,'post-foot');
  foot.append(node('span',`赞 ${post.likes} · 阅读 ${post.views} · 配图 ${post.images.length}${post.editedAt?' · 已编辑':''}`));
  const link=node('a','查看原帖 ↗'); link.href=post.url; link.target='_blank'; link.rel='noopener noreferrer'; foot.append(link);
  card.append(foot); return card;
}
async function get(path) { const r=await fetch(path,{cache:'no-store'}); if(!r.ok) throw new Error('local_read_failed'); return r.json(); }
let busy=false;
async function refresh() {
  if(busy) return; busy=true;
  try {
    const [state,posts,events,qq,images,video,telegram]=await Promise.all([get('/api/status'),get('/api/posts?limit=50'),get('/api/events?limit=30'),get('/api/qq/status'),get('/api/images/status'),get('/api/videos/status'),get('/api/telegram/status')]);
    const qqReady=qq.enabled&&qq.bound&&qq.connection==='connected';
    const qqPermissionMissing=qq.last_error==='40034105';
    $('qqStatus').textContent=!qq.configured?'未配置':!qq.secret_configured?'等待接口密钥':!qq.bound?'等待绑定':qqPermissionMissing?'等待开启主动发送':qq.last_error?'发送需检查':qqReady?(qq.active_send_verified?'已启用':'等待主动推送验证'):'正在连接';
    $('qqDetails').textContent=qq.configured?`机器人对话框 · 已发正文 ${qq.sent_text??0} 条 · 图片 ${qq.sent_images??0} 张 · 待发 ${qq.outbox.pending??0} 条${qq.waiting_image?' · 等待原图下载':''}${qq.last_error?' · 错误码 '+qq.last_error:''}${qqPermissionMissing?' · 请检查机器人主动发送权限':''}${qq.last_test&&!qq.last_error?' · 主动测试已通过':''}`:'尚未配置机器人对话框。';
    $('telegramStatus').textContent=!telegram.configured?'未配置 · 默认关闭':!telegram.enabled?'已配置 · 未启用':telegram.last_error?'发送需检查':telegram.live_delivery_verified?'已启用 · 已有送达记录':'已启用 · 尚未验证送达';
    $('telegramDetails').textContent=telegram.configured?`Telegram 机器人对话框 · 已发正文 ${telegram.sent_text??0} 条 · 原图文件 ${telegram.sent_images??0} 个 · 视频 ${telegram.sent_video_files??0} 个 · 超限链接 ${telegram.sent_video_links??0} 条 · 待发 ${telegram.outbox?.pending??0} 条${telegram.last_error?' · 错误码 '+telegram.last_error:''}`:'功能已预置；自行配置 Token 和会话后启用，当前未连接真实 Telegram。';
    $('imageStatus').textContent=`原图已保存 ${images.counts.ready??0}/${images.total} 张 · ${(images.bytes/1024/1024).toFixed(2)} MiB${images.counts.failed?' · '+images.counts.failed+' 张下载失败，自动重试':''}${images.counts.pending?' · '+images.counts.pending+' 张等待下载':''}`;
    $('videoStatus').textContent=!video.initialized?'等待建立基线':video.last_error?'视频检查异常':video.healthy?(qq.video_notifications_enabled?'监控中 · 群通知已启用':'目录正常 · 群通知未启用'):'等待检查 / 数据已过期';
    $('videoDetails').textContent=`已记录 ${video.stored_videos} 条视频 · 最近检查 ${formatTime(video.last_success)}${video.next_check?' · 下次 '+formatTime(video.next_check*1000):''} · 新视频通知已发 ${qq.sent_videos??0} 条${qq.video_last_test?' · 群通知测试已通过':''}${video.last_error?' · '+video.last_error:''}`;
    const latest=video.latest;
    if(latest) {
      const link=node('a',`最新：${latest.number?'第 '+latest.number+' 期 · ':''}${latest.title}${latest.duration?' · '+latest.duration:''}`);
      link.href=latest.url;link.target='_blank';link.rel='noopener noreferrer';$('latestVideo').replaceChildren(link);
    } else $('latestVideo').textContent='首次建立基线后，只通知之后的新视频与视频信息更新。';
    const download=video.downloads;
    $('videoDownloadStatus').textContent=download?.enabled?`自动下载已启用 · 已保存 ${download.downloaded_files} 个 MP4 · 群内已发 ${qq.sent_video_files??0} 个 · 下载中 ${download.jobs.downloading??0} · 待下载 ${download.jobs.pending??0}${download.latest_job?.status==='downloading'?' · 当前进度 '+download.latest_job.progress.toFixed(0)+'%':''}${download.jobs.failed?' · 下载失败 '+download.jobs.failed+' 个（自动重试）':''}${qq.video_file_error?' · 文件发送待检查 '+qq.video_file_error:''}`:'视频文件自动下载未启用。';
    const file=download?.latest_file;
    if(file){const link=node('a',`${file.display_name||'已下载视频 #'+file.video_id} · ${file.width}×${file.height} · ${(file.size/1000000).toFixed(1)} MB · 打开 MP4`);link.href=file.local_url;link.target='_blank';link.rel='noopener noreferrer';$('videoDownloadLatest').replaceChildren(link);}
    else $('videoDownloadLatest').textContent='';
    $('qqBindHelp').hidden=!(qq.secret_configured&&qq.binding_required);
    $('qqBindCommand').textContent='';
    $('health').textContent=state.healthy?'采集运行正常':state.last_error?'采集异常':'等待采集 / 数据已过期';
    $('health').className=`badge${state.healthy?'':' error'}`;
    $('count').textContent=state.stored_posts; $('readable').textContent=state.readable_archived_posts;
    $('coverage').textContent=`原站当前 ${state.source_total??'—'} 条 · ${state.truncated?'分页未覆盖全部':'已完成本轮分页'}`;
    $('access').textContent=`当前权限：${state.viewer??'未知'}${state.auth_configured?' · 已配置凭据':' · 尚未接入会员'}`;
    $('success').textContent=formatTime(state.last_success);
    $('next').textContent=state.next_poll_at?`下次检查 ${formatTime(state.next_poll_at)}`:'当前未安排下一轮（可能为单次采集）';
    const notice=state.last_error || state.warning || (state.last_locked_count?'新帖监控可用。会员正文尚未取得，接入本机会员凭据后将自动补存。':null);
    $('notice').hidden=!notice; $('notice').textContent=notice??'';
    $('posts').replaceChildren(...(posts.posts.length?posts.posts.map(postCard):[node('p','还没有采集到帖子。','empty')]));
    $('events').replaceChildren(...events.events.map(event=>{
      const item=node('div',undefined,`event ${event.type==='new_post'?'new':event.type}`);
      item.append(node('strong',`${labels[event.type]??event.type}${event.postId?' · #'+event.postId:''}`),node('time',formatTime(event.observedAt)));
      if(event.type==='baseline') item.append(node('p',`归档 ${event.details.count} 条既有帖子，后续新增将单独记录。`));
      if(event.type==='video_baseline') item.append(node('p',`记录 ${event.details.count} 条既有视频，之后更新才通知。`));
      if(event.details.video) item.append(node('p',event.details.video.title));
      if(event.details.message) item.append(node('p',event.details.message));
      if(event.type==='access_changed') item.append(node('p',`${event.details.previous} → ${event.details.viewer}`));
      return item;
    }));
  } catch(error) { $('health').textContent='本地服务未连接'; $('health').className='badge error'; }
  finally { busy=false; }
}
$('refresh').addEventListener('click',refresh);
refresh(); setInterval(refresh,10000);
$('connect').addEventListener('click',()=>{ $('loginPanel').hidden=!$('loginPanel').hidden; if(!$('loginPanel').hidden) $('loginEmail').focus(); });
$('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const button=$('loginSubmit'); button.disabled=true; button.textContent='正在登录并验证…';
  $('loginResult').textContent='正在向原站登录，请稍候。';
  try {
    const session=await get('/api/auth/session');
    const payload=JSON.stringify({email:$('loginEmail').value.trim(),password:$('loginPassword').value});
    $('loginPassword').value='';
    const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},body:payload});
    const result=await response.json();
    $('loginResult').textContent=response.ok?result.message:(result.error||'登录未完成，请重试。');
    if(response.ok) await refresh();
  } catch(error) { $('loginResult').textContent='本地服务连接失败，请刷新页面后再试。'; }
  finally { $('loginPassword').value=''; button.disabled=false; button.textContent='登录并接入监控'; }
});
