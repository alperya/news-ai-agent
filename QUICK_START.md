# ⚡ Quick Start - Instagram Token Auto-Refresh

## 🎯 5 Dakika Kurulumu

### Durum
✅ **Sistem hazır ve çalışıyor!**
- Token validation: Working
- Token caching: Active
- Instagram Publisher: Integrated
- Fallback mechanism: Ready

### Hemen Kullan

```bash
# Test et (Dry-run)
python3 main.py --dry-run --platform instagram

# Canlı posting
python3 main.py --no-dry-run --platform instagram
```

---

## 🔄 Otomatik Refresh (Opsiyonel)

Manuel token güncellemelerini tamamen ortadan kaldırmak için:

1. **Facebook Developer Console'dan Credentials Al:**
   - https://developers.facebook.com/apps/
   - App ID ve Secret kopyala

2. **.env'ye Ekle:**
   ```env
   INSTAGRAM_CLIENT_ID=your_app_id
   INSTAGRAM_CLIENT_SECRET=your_app_secret
   ```

3. **Test Et:**
   ```bash
   python3 -c "from social_publisher import InstagramPublisher; InstagramPublisher()"
   ```

   Mesaj görmen gerekir:
   ```
   ✅ Token Refresh: Enabled
   ```

---

## 📊 Sistem Durumu

```
🔐 Token: ✅ Valid and cached
📁 Cache: ✅ Active (.instagram_token_cache.json)
🤖 Publisher: ✅ Token manager integrated
⚙️ Refresh: ⚠️ Optional (no credentials yet)
```

---

## ❓ Sorularım var!

- **Token ne zaman update olur?**
  → Startup'ta, publishing öncesi, otomatik olarak

- **Credentials yoksa sistem çalışır mı?**
  → Evet! Fallback mekanizması var

- **Cache'i sıfırlamak istersen?**
  ```bash
  rm -f .instagram_token_cache.json
  ```

---

## 📚 Detaylı Rehberler

- 📖 `INSTALLATION_COMPLETE.md` - Tüm detaylar
- 📖 `SETUP_TOKEN_REFRESH.md` - Token refresh kurulumu
- 📖 `GET_FACEBOOK_CREDENTIALS.md` - Credentials alma
- 📖 `TOKEN_REFRESH_STATUS.md` - FAQ ve troubleshooting

---

**👉 Şu anda:** `python3 main.py --dry-run` çalıştır!
