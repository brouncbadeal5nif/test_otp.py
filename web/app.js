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

let activeSessions = [];
let selectedService = null;

// Xác nhận thuê số (Mở Modal chọn mạng)
function confirmRent(service) {
    selectedService = service;
    document.getElementById('modal-service-name').textContent = service.Name;
    document.getElementById('modal-service-price').textContent = `${service.Cost.toLocaleString('vi-VN')}đ`;
    document.getElementById('carrier-modal').classList.remove('hidden');
}

function closeCarrierModal() {
    document.getElementById('carrier-modal').classList.add('hidden');
    selectedService = null;
}

// Bấm chọn mạng và Thuê
function submitRent(carrier) {
    if (!selectedService) return;
    closeCarrierModal();
    rentNumber(selectedService, carrier);
}

// Gọi API thuê số
async function rentNumber(service, carrier) {
    tg.MainButton.showProgress();
    try {
        const payload = { 
            app_id: service.Id, 
            app_name: service.Name, 
            cost: service.Cost,
            carrier: carrier
        };
        
        const res = await fetch(`${API_BASE}/rent`, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                'X-TG-INIT-DATA': tg.initData 
            },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        tg.MainButton.hideProgress();

        if (data.ok) {
            showToast('Thành công! Hãy kiểm tra tin nhắn Bot.');
            setTimeout(() => {
                tg.close();
            }, 1500);
        } else {
            tg.showAlert(data.message || 'Không thể thuê số lúc này');
        }
    } catch (err) {
        tg.MainButton.hideProgress();
        tg.showAlert('Lỗi kết nối. Vui lòng thử lại.');
    }
}

// Copy to Clipboard
function copyToClipboard(elementId, typeName) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const text = el.textContent;
    if (text === '------' || text === 'Đang lấy số...') return;

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
