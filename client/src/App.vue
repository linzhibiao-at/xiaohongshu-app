<script>
export default {
  onLaunch() {},
}
</script>

<style>
page { background: #f5f6f8; color: #222; font-size: 14px; }
.page { padding: 12px; }
.card { background: #fff; border-radius: 10px; padding: 14px; margin-bottom: 12px; }
.title { font-size: 16px; font-weight: 600; margin-bottom: 10px; }
.row { display: flex; align-items: center; justify-content: space-between; }
.muted { color: #888; font-size: 12px; }
.input, .textarea { background: #f5f6f8; border-radius: 8px; padding: 10px; margin-bottom: 10px; font-size: 14px; }
.input { height: 40px; box-sizing: border-box; }
.textarea { width: 100%; box-sizing: border-box; min-height: 90px; }
.btn { background: #ff2442; color: #fff; border-radius: 8px; font-size: 15px; line-height: 42px; }
.btn::after { border: none; }
.btn[disabled] { opacity: 0.5; }
.btn-ghost { background: #fff; color: #ff2442; border: 1px solid #ff2442; }
.btn-sm { font-size: 13px; line-height: 30px; padding: 0 12px; margin: 0 0 0 8px; display: inline-block; }
.tag { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #f0f1f3; color: #555; margin-right: 6px; }
.tag-ok { background: #e7f7ed; color: #1a9a4b; }
.tag-warn { background: #fff4e0; color: #c77700; }
.tag-bad { background: #fde8ea; color: #d6213a; }
.picker { background: #f5f6f8; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
.empty { text-align: center; color: #999; padding: 30px 0; }
</style>
