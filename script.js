// 페이지별 데이터 (보통은 서버에서 가져오지만, 여기선 변수에 저장)
const pages = {
    home: `
        <h1 class="page-title">환영합니다!</h1>
        <p>도시의 소음에서 벗어나 별빛 아래 휴식을 취하세요.</p>
        <img src="https://images.unsplash.com/photo-1566073771259-6a8506099945?auto=format&fit=crop&w=600&q=80" style="width:100%; border-radius:15px; margin-top:20px;">
    `,
    rooms: `
        <h1 class="page-title">Our Rooms</h1>
        <div class="card-container">
            <div class="card"><h3>A-101</h3><p>오션뷰</p></div>
            <div class="card"><h3>B-201</h3><p>풀빌라</p></div>
            <div class="card"><h3>C-301</h3><p>단체룸</p></div>
        </div>
    `,
    contact: `
        <h1 class="page-title">Contact Us</h1>
        <p>전화번호: 010-1234-5678</p>
        <p>카카오톡: starlight_stay</p>
        <button style="margin-top:20px; padding:10px 20px;">실시간 채팅 상담</button>
    `
};

// 메뉴 클릭 시 실행될 함수
function router(pageName) {
    const contentDiv = document.getElementById('content');
    
    // 부드러운 전환 효과를 위해 살짝 투명하게 만들기
    contentDiv.style.opacity = 0;

    setTimeout(() => {
        // 실제 데이터 교체
        contentDiv.innerHTML = pages[pageName] || "<h1>404</h1><p>페이지를 찾을 수 없습니다.</p>";
        contentDiv.style.opacity = 1;
    }, 200);
}

// 처음 접속했을 때 'home' 화면 보여주기
window.onload = () => router('home');