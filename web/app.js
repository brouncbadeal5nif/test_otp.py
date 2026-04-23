const tg = window.Telegram.WebApp;
tg.expand(); // Expand to full height

const API_BASE = '/api/mini';
let currentSessionId = null;
let pollInterval = null;
let allServices = [];

// Khởi tạo
document.addEventListener('DOMContentLoaded', () => {
    // Hiển thị tên user nếu chạy trong Telegram
    if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
        document.getElementById('user-name').textContent = tg.initDataUnsafe.user.first_name || 'Khách';
    }

    // Cấu hình Header color cho khớp theme
    tg.setHeaderColor('secondary_bg_color');

    // Fetch dữ liệu
    fetchUserInfo();
    fetchServices();

    // Event listener cho tìm kiếm
    document.getElementById('search-input').addEventListener('input', (e) => {
        renderServices(e.target.value);
    });

    // Event listener cho nút Hủy
    document.getElementById('btn-cancel-session').addEventListener('click', cancelSession);
});

// Fetch thông tin user (Số dư)
async function fetchUserInfo() {
    try {
        const res = await fetch(`${API_BASE}/me`, {
            headers: { 'X-TG-INIT-DATA': tg.initData }
        });
        const data = await res.json();
        if (data.ok) {
            document.getElementById('balance-value').textContent = data.balance.toLocaleString('vi-VN');
        } else {
            showToast(data.message || 'Lỗi lấy thông tin');
        }
    } catch (err) {
        console.error(err);
    }
}

// Fetch danh sách dịch vụ
async function fetchServices() {
    try {
        const res = await fetch(`${API_BASE}/services`, {
            headers: { 'X-TG-INIT-DATA': tg.initData }
        });
        const data = await res.json();
        if (data.ok) {
            allServices = data.services;
            renderServices();
        } else {
            document.getElementById('service-list').innerHTML = `<div class="loading-services">Lỗi tải dịch vụ</div>`;
        }
    } catch (err) {
        console.error(err);
        document.getElementById('service-list').innerHTML = `<div class="loading-services">Lỗi mạng</div>`;
    }
}

// Map Tên dịch vụ sang Icon FontAwesome
function getIconForService(name) {
    const n = name.toLowerCase();
    if (n.includes('facebook')) return 'fa-brands fa-facebook';
    if (n.includes('google') || n.includes('gmail')) return 'fa-brands fa-google';
    if (n.includes('telegram')) return 'fa-brands fa-telegram';
    if (n.includes('tiktok')) return 'fa-brands fa-tiktok';
    if (n.includes('instagram')) return 'fa-brands fa-instagram';
    if (n.includes('twitter')) return 'fa-brands fa-twitter';
    if (n.includes('shopee')) return 'fa-solid fa-bag-shopping';
    if (n.includes('apple')) return 'fa-brands fa-apple';
    if (n.includes('paypal')) return 'fa-brands fa-paypal';
    if (n.includes('amazon')) return 'fa-brands fa-amazon';
    if (n.includes('wechat')) return 'fa-brands fa-weixin';
    if (n.includes('youtube')) return 'fa-brands fa-youtube';
    if (n.includes('whatsapp')) return 'fa-brands fa-whatsapp';
    return 'fa-solid fa-comment-sms';
}

// Render lưới dịch vụ
function renderServices(filter = '') {
    const listEl = document.getElementById('service-list');
    listEl.innerHTML = '';

    const filtered = allServices.filter(s => s.Name.toLowerCase().includes(filter.toLowerCase()));

    if (filtered.length === 0) {
        listEl.innerHTML = `<div class="loading-services">Không tìm thấy dịch vụ</div>`;
        return;
    }

    filtered.forEach(service => {
        const card = document.createElement('div');
        card.className = 'service-card';
        card.onclick = () => confirmRent(service);

        card.innerHTML = `
            <div class="service-icon">
                <i class="${getIconForService(service.Name)}"></i>
            </div>
            <div class="service-name">${service.Name}</div>
            <div class="service-price">${service.Cost.toLocaleString('vi-VN')}đ</div>
        `;
        listEl.appendChild(card);
    });
}

// Xác nhận thuê số
function confirmRent(service) {
    if (currentSessionId) {
        tg.showConfirm('Bạn đang có 1 số chưa lấy xong mã. Hủy số cũ để thuê mới?', (confirmed) => {
            if (confirmed) {
                cancelSession();
                rentNumber(service);
            }
        });
        return;
    }

    tg.showConfirm(`Bạn muốn thuê số cho ${service.Name} với giá ${service.Cost.toLocaleString('vi-VN')}đ?`, (confirmed) => {
        if (confirmed) rentNumber(service);
    });
}

// Gọi API thuê số
async function rentNumber(service) {
    tg.MainButton.showProgress();
    try {
        const res = await fetch(`${API_BASE}/rent`, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                'X-TG-INIT-DATA': tg.initData 
            },
            body: JSON.stringify({ app_id: service.Id, app_name: service.Name, cost: service.Cost })
        });
        const data = await res.json();
        tg.MainButton.hideProgress();

        if (data.ok) {
            showActiveSession(service.Name, data.number, data.request_id);
            fetchUserInfo(); // Cập nhật lại số dư bị trừ
        } else {
            tg.showAlert(data.message || 'Không thể thuê số lúc này');
        }
    } catch (err) {
        tg.MainButton.hideProgress();
        tg.showAlert('Lỗi kết nối. Vui lòng thử lại.');
    }
}

// Hiển thị khu vực Session
function showActiveSession(serviceName, phone, reqId) {
    currentSessionId = reqId;
    
    document.getElementById('active-session').classList.remove('hidden');
    document.getElementById('active-service-name').textContent = serviceName;
    document.getElementById('phone-value').textContent = phone;
    
    // Reset OTP UI
    document.getElementById('otp-display').classList.add('hidden');
    document.getElementById('otp-value').textContent = '------';
    document.getElementById('otp-indicator').className = 'status-indicator waiting';
    document.getElementById('otp-text').textContent = 'Đang chờ tin nhắn...';
    
    // Cuộn xuống cuối
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

    // Bắt đầu Polling
    startPolling(reqId);
}

// Polling kiểm tra mã OTP
function startPolling(reqId) {
    if (pollInterval) clearInterval(pollInterval);
    
    pollInterval = setInterval(async () => {
        try {
            const res = await fetch(`${API_BASE}/status/${reqId}`, {
                headers: { 'X-TG-INIT-DATA': tg.initData }
            });
            const data = await res.json();
            
            if (data.ok && data.status === 'completed') {
                clearInterval(pollInterval);
                pollInterval = null;
                currentSessionId = null; // Hoàn tất
                
                // Hiển thị mã
                document.getElementById('otp-indicator').className = 'status-indicator success';
                document.getElementById('otp-text').textContent = 'Đã có mã xác nhận!';
                document.getElementById('otp-display').classList.remove('hidden');
                document.getElementById('otp-value').textContent = data.code;
                
                tg.HapticFeedback.notificationOccurred('success');
            } else if (!data.ok || data.status === 'cancelled') {
                clearInterval(pollInterval);
                pollInterval = null;
                currentSessionId = null;
                document.getElementById('otp-indicator').className = 'status-indicator';
                document.getElementById('otp-indicator').style.backgroundColor = 'var(--danger-color)';
                document.getElementById('otp-text').textContent = 'Số bị hủy hoặc lỗi mạng.';
                fetchUserInfo(); // Hoàn tiền
            }
        } catch (err) {
            console.error(err);
        }
    }, 5000); // Check mỗi 5s
}

// Hủy Session
function cancelSession() {
    if (!currentSessionId) return;
    
    // Gọi API hủy số nếu hệ thống hỗ trợ, tạm thời chỉ ẩn giao diện
    clearInterval(pollInterval);
    pollInterval = null;
    currentSessionId = null;
    
    document.getElementById('active-session').classList.add('hidden');
    showToast('Đã hủy chờ số');
}

// Copy to Clipboard
function copyToClipboard(elementId, typeName) {
    const text = document.getElementById(elementId).textContent;
    if (text === '------' || text === 'Đang lấy số...') return;

    // Use fallback copy logic as navigator.clipboard might be restricted in some TG clients
    const textarea = document.createElement('textarea');
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand('copy');
        showToast(`Đã copy ${typeName}`);
        tg.HapticFeedback.selectionChanged();
    } catch (err) {
        console.error('Lỗi copy', err);
    }
    document.body.removeChild(textarea);
}

// Toast Notification
function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.classList.remove('hidden');
    setTimeout(() => {
        toast.classList.add('hidden');
    }, 2500);
}
